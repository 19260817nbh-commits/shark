"""Read low-rate auto-aim diagnostics from a log without touching serial/control."""
from collections import deque
from datetime import datetime
from pathlib import Path
import math
import re
import threading
import time

ANSI = re.compile(r'\x1b\[[0-9;]*m')
STAMP = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)\]')
PAIRS = re.compile(r'([a-z_]+)=([^\s]+)')


class DiagnosticStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        with self.lock:
            self.latest = {}
            self.history = deque(maxlen=80)
            self.lines = self.ignored = self.malformed = 0
            self.last_issue = -math.inf

    def add_line(self, raw, fallback_age=0.):
        with self.lock:
            self.lines += 1
            # Do not run regular expressions over high-frequency timing output.
            if not any(tag in raw for tag in ('[AimTrace]', '[ControlTrace]', 'Unsolvable target',
                                               '[warning]', '[error]', '[critical]', 'Switch to ')):
                self.ignored += 1
                return
            line = ANSI.sub('', raw).strip()[:4096]
            age = fallback_age
            stamp = STAMP.match(line)
            if stamp:
                try:
                    age = max(0., time.time()-datetime.fromisoformat(stamp[1]).timestamp())
                except ValueError:
                    pass
            observed = time.monotonic()-age
            fields = dict(PAIRS.findall(line))
            if '[AimTrace]' in line:
                kind = 'aim'
                required = ('rx_color', 'detected', 'red', 'blue', 'kept', 'targets')
                try:
                    data = {key:int(fields[key]) for key in required}
                    data.update(active_color=fields['active_color'], tracker=fields['tracker'], rx_mode=fields['rx_mode'])
                    if any(data[key] < 0 for key in required):
                        raise ValueError()
                except (ValueError, KeyError):
                    self.malformed += 1
                    return
            elif '[ControlTrace]' in line and 'reason=' in line:
                kind = 'control'
                try:
                    data = {key:fields[key] == 'true' for key in ('target', 'control', 'planner_fire')}
                    if any(fields[key] not in ('true', 'false') for key in data):
                        raise ValueError()
                    data.update(target_id=int(fields['target_id']), yaw=float(fields['yaw']),
                                pitch=float(fields['pitch']), reason=fields['reason'])
                    if not all(math.isfinite(data[key]) for key in ('yaw', 'pitch')):
                        raise ValueError()
                except (ValueError, KeyError):
                    self.malformed += 1
                    return
            elif '[ControlTrace]' in line:
                kind, data = 'enabled', {}
            else:
                kind, data = 'issue', {}
            item = dict(kind=kind, fields=data, text=line, observed=observed)
            self.latest[kind] = item
            if kind == 'issue' and time.monotonic()-self.last_issue < .25:
                return
            if kind == 'issue':
                self.last_issue = time.monotonic()
            self.history.append(item)

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            def public(item):
                return dict(kind=item['kind'], fields=item['fields'], text=item['text'],
                            age_s=max(0., now-item['observed']))
            return dict(latest={key:public(item) for key,item in self.latest.items()},
                        history=[public(item) for item in reversed(self.history)],
                        lines=self.lines, ignored=self.ignored, malformed=self.malformed)

    def export(self):
        with self.lock:
            return ('\n'.join(item['text'] for item in self.history)+'\n').encode('utf-8')


class DiagnosticFollower:
    def __init__(self, path, store):
        self.path = Path(path).resolve()
        self.store = store
        self.stop = threading.Event()
        self.error = ''
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        stream = None
        identity = None
        pending = bytearray()
        try:
            while not self.stop.is_set():
                if self.path.is_file():
                    stat = self.path.stat()
                    current = (stat.st_dev, stat.st_ino)
                    if stream is None or current != identity or stat.st_size < stream.tell():
                        if stream:
                            stream.close()
                        stream = self.path.open('rb')
                        identity = current
                        pending.clear()
                        self.store.reset()
                    data = stream.read(256*1024)
                    if data:
                        pending.extend(data)
                        lines = pending.split(b'\n')
                        pending = bytearray(lines.pop())
                        # A corrupt/huge unterminated line must not grow memory forever.
                        if len(pending) > 65536:
                            pending.clear()
                        fallback_age = max(0., time.time()-stat.st_mtime)
                        for line in lines:
                            self.store.add_line(line.decode('utf-8', errors='replace'), fallback_age)
                        continue
                self.stop.wait(.1)
        except OSError as exc:
            self.error = str(exc)
        finally:
            if stream:
                stream.close()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=3)


def explain_control(state, wire):
    latest = state['latest']
    aim, control = latest.get('aim'), latest.get('control')
    if not control:
        return dict(code='waiting', title='等待控制诊断', detail='需编译诊断补丁；联合启动脚本会自动开启 SP_CONTROL_DEBUG=1。')
    if control['age_s'] > 3:
        return dict(code='stale', title='控制诊断已停止更新', detail='请检查自瞄进程；旧状态不代表当前控制结果。')
    reason = control['fields']['reason']
    if reason == 'no_target':
        if not aim or aim['age_s'] > 3:
            return dict(code='aim_stale', title='无目标，检测状态未更新', detail='检查主循环是否卡在相机取帧或姿态对齐。')
        data = aim['fields']
        if data['detected'] == 0:
            return dict(code='no_detection', title='检测器没有返回装甲板', detail='检查目标是否入镜、模型识别和置信度。')
        if data['kept'] == 0:
            return dict(code='filtered', title='装甲板被过滤', detail='核对实际使用颜色 '+data['active_color']+'，以及前哨等过滤条件。')
        if data['targets'] == 0:
            return dict(code='tracking', title='跟踪器未输出有效目标', detail='跟踪状态：'+data['tracker']+'；检查丢失、发散及收敛日志。')
        return dict(code='handoff', title='规划线程仍显示空目标', detail='检测与规划采样不同步；持续出现时检查目标队列。')
    if reason == 'planner_rejected':
        return dict(code='planner', title='有目标，但规划未通过', detail='查看下方 Unsolvable target 等异常，检查位姿、距离和弹道求解。')
    tx = wire.get('tx')
    if tx and tx.get('age_s', 99) <= 1:
        if tx['values'].get('mode') == 0:
            return dict(code='send_mismatch', title='规划允许控制，但 TX 仍不控制', detail='核对 gimbal.send 第一个参数及是否查看同一进程的日志。')
        return dict(code='sent', title='已发送云台控制指令', detail='当前 TX mode='+str(tx['values'].get('mode'))+'；实际是否跟随请对照 RX。')
    return dict(code='no_tx', title='规划已允许控制，等待最新 TX', detail='检查日志录制与串口写入是否仍在更新。')
