"""Sample-based telemetry measurements; no changes to control or wire data."""
from bisect import bisect_right
import math
from statistics import median


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    index = (len(values)-1)*fraction
    lo = int(index)
    return values[lo]+(values[min(lo+1, len(values)-1)]-values[lo])*(index-lo)


def sampling_stats(frames):
    result = {}
    for direction in ('tx', 'rx'):
        stamps = sorted(set(f['t'] for f in frames if f['direction'] == direction))
        gaps = [(b-a)*1000 for a,b in zip(stamps, stamps[1:]) if b > a]
        typical = median(gaps) if gaps else None
        result[direction] = dict(count=len(stamps), median_ms=typical,
                                 p95_ms=percentile(gaps, .95), max_ms=max(gaps) if gaps else None,
                                 effective_hz=1000/typical if typical else None)
    return result


def aligned_errors(frames, start, end):
    """At each RX timestamp use the latest prior TX, held for <=50 ms.

    No future command, extrapolation, or estimated lag compensation is used.
    Rates are intentionally not mixed into a single 'simultaneous' sample.
    """
    tx = sorted((f for f in frames if f['direction'] == 'tx'), key=lambda f:f['t'])
    times = [f['t'] for f in tx]
    errors = []
    for rx in frames:
        if rx['direction'] != 'rx' or not start <= rx['t'] <= end:
            continue
        i = bisect_right(times, rx['t'])-1
        if i < 0:
            continue
        command = tx[i]
        age = rx['t']-command['t']
        if age > .05 or command['values'].get('mode', 0) not in (1, 2):
            continue
        a,b = command['values'],rx['values']
        if not all(math.isfinite(v.get(key, math.nan)) for v in (a,b) for key in ('yaw','pitch')):
            continue
        errors.append(dict(t=rx['t'], tx_t=command['t'], age_ms=age*1000,
                           yaw=math.remainder(a['yaw']-b['yaw'], 2*math.pi),
                           pitch=a['pitch']-b['pitch']))
    return sorted(errors, key=lambda p:p['t'])


def response_summary(frames, start, end):
    errors = aligned_errors(frames, start, end)
    summary = dict(start=start, end=end, samples=len(errors),
                   method='RX采样时刻，对齐此前最近TX；指令保持≤50 ms，Yaw取最短角差')
    for axis in ('yaw','pitch'):
        values = [e[axis] for e in errors]
        summary[axis] = dict(rmse=math.sqrt(sum(v*v for v in values)/len(values)) if values else None,
                             bias=sum(values)/len(values) if values else None,
                             p95_abs=percentile([abs(v) for v in values], .95),
                             max_abs=max(map(abs, values)) if values else None)
    summary['sampling'] = sampling_stats([f for f in frames if start <= f['t'] <= end])
    summary['max_alignment_age_ms'] = max((p['age_ms'] for p in errors), default=None)
    return summary


def step_response(frames, start, end, axis):
    """Conservative isolated-step metrics; reject moving/ambiguous setpoints.

    Threshold crossing times use sample intervals, never claim device-time precision.
    """
    tx = sorted((f for f in frames if f['direction']=='tx' and start<=f['t']<=end),key=lambda f:f['t'])
    rx = sorted((f for f in frames if f['direction']=='rx' and start<=f['t']<=end),key=lambda f:f['t'])
    invalid = dict(status='unavailable', reason='窗口内需要一个清晰阶跃及前后稳定段')
    if len(tx)<10 or len(rx)<10 or any(f['values'].get('mode',0) not in (1,2) for f in tx):
        return invalid
    def unwrap(samples):
        result=[(samples[0]['t'],samples[0]['values'][axis])]
        for f in samples[1:]:
            raw=f['values'][axis]
            previous=samples[len(result)-1]['values'][axis]
            value=result[-1][1]+math.remainder(raw-previous,2*math.pi) if axis=='yaw' else raw
            result.append((f['t'],value))
        return result
    commands,feedback=unwrap(tx),unwrap(rx)
    index=max(range(1,len(commands)),key=lambda i:abs(commands[i][1]-commands[i-1][1]))
    t0=commands[index][0]
    before=[p for p in commands if t0-.3<=p[0]<t0]
    after=[p for p in commands if p[0]>=t0]
    if len(before)<3 or len(after)<5 or before[-1][0]-before[0][0]<.1 or after[-1][0]-t0<.3:
        return invalid
    a=median(p[1] for p in before);b=median(p[1] for p in after);step=b-a
    if abs(step)<math.radians(.3):
        return invalid
    tolerance=max(math.radians(.05),abs(step)*.02)
    if max(abs(v-a) for _,v in before)>tolerance or max(abs(v-b) for _,v in after)>tolerance:
        return dict(invalid,reason='指令仍在变化，不能按单次阶跃计算超调')
    if any(y[0]-x[0]>.1 for x,y in zip(commands,commands[1:])) or any(y[0]-x[0]>.1 for x,y in zip(feedback,feedback[1:])):
        return dict(invalid,reason='阶跃窗口采样间断过大')
    initial=[v for t,v in feedback if t0-.15<=t<t0]
    if not initial:
        return invalid
    offset=round((a-median(initial))/(2*math.pi))*2*math.pi if axis=='yaw' else 0
    if abs(median(initial)+offset-a)>max(tolerance,abs(step)*.1):
        return dict(invalid,reason='阶跃前反馈未接近初始指令，无法可靠计算')
    response=[(t,v+offset) for t,v in feedback if t>=t0]
    if not response:
        return invalid
    directed=[(t,(v-a)/step) for t,v in response]
    crossing=lambda threshold: next((t for t,fraction in directed if fraction>=threshold),None)
    t10,t90=crossing(.1),crossing(.9)
    overshoot=max(0., max(fraction for _,fraction in directed)-1)*100
    outside=[i for i,(_,v) in enumerate(response) if abs(v-b)>tolerance]
    first_settled=outside[-1]+1 if outside else 0
    settled=None
    if first_settled<len(response) and response[-1][0]-response[first_settled][0]>=.2:
        settled=(response[first_settled][0]-t0)*1000
    return dict(status='ok',step_time=t0,step_rad=step,overshoot_percent=overshoot,
                rise_ms=(t90-t10)*1000 if t10 is not None and t90 is not None else None,
                settling_ms=settled,tolerance_rad=tolerance,
                observed_ms=(response[-1][0]-t0)*1000,
                reason='采样阶跃测量；稳定时间要求持续带内≥200 ms，不推断阻尼系数')
