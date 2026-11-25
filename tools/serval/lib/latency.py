"""Estimate closed-loop following lag from recorded angles, not serial RTT."""
from bisect import bisect_right
import math
from statistics import median

STEP = .01
MAX_LAG = .5
WINDOW = 5.0


def _samples(frames, direction, axis):
    unique = {}
    for frame in frames:
        if frame['direction'] == direction:
            value = frame['values'].get(axis)
            if value is not None and math.isfinite(value):
                unique[frame['t']] = value
    samples = sorted(unique.items())
    if axis == 'yaw' and samples:
        unwrapped = [samples[0]]
        previous = samples[0][1]
        for stamp, value in samples[1:]:
            unwrapped.append((stamp, unwrapped[-1][1] + math.remainder(value-previous, 2*math.pi)))
            previous = value
        return unwrapped
    return samples


def _resample(samples, start, count, step=STEP, max_gap=.1):
    times = [p[0] for p in samples]
    result = []
    for i in range(count):
        t = start+i*step
        j = bisect_right(times, t)-1
        if j >= 0 and abs(t-times[j]) < 1e-7:
            result.append(samples[j][1])
        elif j < 0 or j+1 >= len(samples) or times[j+1]-times[j] > max_gap:
            result.append(None)
        else:
            ratio = (t-times[j])/(times[j+1]-times[j])
            result.append(samples[j][1] + ratio*(samples[j+1][1]-samples[j][1]))
    return result


def _axis_delay(frames, axis):
    tx, rx = _samples(frames, 'tx', axis), _samples(frames, 'rx', axis)
    missing = dict(delay_ms=None, correlation=None, range_ms=None, samples=0)
    if len(tx) < 20 or len(rx) < 20:
        return dict(missing, status='insufficient_data', reason='等待至少约 1 秒有效收发数据')
    start = max(tx[0][0], rx[0][0], min(tx[-1][0], rx[-1][0])-WINDOW-MAX_LAG)
    end = min(tx[-1][0], rx[-1][0]-MAX_LAG)
    tx_interval = median(b[0]-a[0] for a,b in zip(tx,tx[1:]))
    rx_interval = median(b[0]-a[0] for a,b in zip(rx,rx[1:]))
    sample_interval = max(tx_interval, rx_interval)
    step = max(.001, min(.005, sample_interval/4))
    n = int((end-start)/step)+1
    if end-start < .8:
        return dict(missing, status='insufficient_data', reason='连续控制时间不足')
    max_shift = round(MAX_LAG/step)
    x = _resample(tx, start, n, step, max_gap=min(.1, tx_interval*5))
    y = _resample(rx, start, n+max_shift, step, max_gap=min(.1, rx_interval*5))
    actual_x = [v for v in x if v is not None]
    if len(actual_x) < n*.8:
        return dict(missing, status='gaps', reason='采样间断过大，无法估计')
    if max(actual_x)-min(actual_x) < math.radians(.2):
        return dict(missing, status='no_motion', reason='指令基本静止，无法估计跟随滞后')
    scores = []
    def score_shift(shift):
        pairs = [(a, b) for a, b in zip(x, y[shift:shift+n]) if a is not None and b is not None]
        if len(pairs) < max(60, n*.8):
            return
        mean_x = sum(p[0] for p in pairs)/len(pairs)
        mean_y = sum(p[1] for p in pairs)/len(pairs)
        xx = yy = xy = 0.
        for a, b in pairs:
            a, b = a-mean_x, b-mean_y
            xx += a*a
            yy += b*b
            xy += a*b
        if xx > 1e-12 and yy > 1e-12:
            scores.append((max(-1., min(1., xy/math.sqrt(xx*yy))), shift, len(pairs)))
    # Search coarsely first; refine locally without interpolating the final peak.
    coarse = max(1, round(.01/step))
    coarse_shifts = set(range(0,max_shift+1,coarse)) | {max_shift}
    for shift in sorted(coarse_shifts):
        score_shift(shift)
    if scores:
        best_shift = max(scores, key=lambda p:p[0])[1]
        for shift in range(max(0,best_shift-coarse), min(max_shift,best_shift+coarse)+1):
            if shift not in coarse_shifts:
                score_shift(shift)
    if not scores:
        return dict(missing, status='no_response', reason='反馈基本静止或有效样本不足')
    score, shift, count = max(scores, key=lambda p: p[0])
    base = dict(missing, correlation=score, samples=count, search_step_ms=step*1000,
                sample_interval_ms=sample_interval*1000)
    if score < .75:
        return dict(base, status='low_correlation', reason='指令和反馈相关性不足，检查控制、坐标或振荡')
    if max(p[0] for p in scores)-min(p[0] for p in scores) < .002:
        return dict(base, status='ambiguous', reason='运动形状不足以区分延迟，需有加减速或方向变化')
    near = [p[1] for p in scores if p[0] >= score-.002]
    if (max(near)-min(near))*step > .3:
        return dict(base, status='ambiguous', reason='相关峰过宽，无法给出可靠估计')
    if shift == max_shift:
        return dict(base, status='out_of_range', reason='最佳值位于 500 ms 搜索边界，无法确定')
    return dict(base, status='ok', delay_ms=round(shift*step*1000, 2),
                range_ms=[round(min(near)*step*1000, 2), round(max(near)*step*1000, 2)],
                reason='角度曲线相关估计，包含传输、控制和机械响应；不是串口 RTT')


def estimate_follow_delay(frames, now):
    result = dict(step_ms=round(STEP*1000), max_lag_ms=round(MAX_LAG*1000), window_s=WINDOW)
    latest = {}
    for frame in frames:
        latest[frame['direction']] = frame
    reason = None
    if not all(direction in latest for direction in ('tx', 'rx')):
        reason = ('insufficient_data', '等待 TX 和 RX 数据')
    elif any(now-latest[d]['t'] > .5 for d in ('tx', 'rx')):
        reason = ('stale', '数据已过期，停止估计')
    elif latest['tx']['values'].get('mode', 0) == 0:
        reason = ('not_controlled', 'TX mode=0，当前未控制云台')
    if reason:
        for axis in ('yaw', 'pitch'):
            result[axis] = dict(status=reason[0], reason=reason[1], delay_ms=None,
                                correlation=None, range_ms=None, samples=0)
        return result
    # Do not estimate across an interval in which the host relinquished control.
    cutoff = max((f['t'] for f in frames if f['direction'] == 'tx'
                  and f['values'].get('mode', 0) == 0), default=-math.inf)
    previous_target = None
    for frame in frames:
        if frame['direction'] == 'tx':
            target = frame['values'].get('target_id')
            if previous_target is not None and target != previous_target:
                cutoff = max(cutoff, frame['t'])
            previous_target = target
    active = [f for f in frames if f['t'] > cutoff]
    for axis in ('yaw', 'pitch'):
        result[axis] = _axis_delay(active, axis)
    return result
