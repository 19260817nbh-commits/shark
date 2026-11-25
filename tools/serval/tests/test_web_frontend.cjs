'use strict';
/* Frontend logic tests (no browser): SSE/UDP pure helpers in signals.js and the
   /api/native/* quick-action client in native.js, driven through a fake DOM. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const S = require('../web/signals.js');

/* ---------- signals.js: SSE payload parsing + UDP counter normalisation ---------- */
assert.equal(S.parseSnapshot('{"a":1}').a, 1);
assert.equal(S.parseSnapshot('not json'), null);
assert.equal(S.parseSnapshot('[]'), null);
assert.equal(S.parseSnapshot(''), null);
assert.deepEqual(S.udpCounters(null), {
  missing: 0, late: 0, invalid: 0, peer: '—', error: '', age: null, fps: null,
  fpsAge: null, session: null, running: null
});
const flat = S.udpCounters({missing: 2, late: 1, bad: 3, peer: '127.0.0.1:9871',
  error: 'recv error', age: 0.5, fps: 30.5, fps_age: 0.2, session: 42, running: true});
assert.equal(flat.missing, 2);
assert.equal(flat.late, 1);
assert.equal(flat.invalid, 3, 'bad falls back to invalid');
assert.equal(flat.peer, '127.0.0.1:9871');
assert.equal(flat.error, 'recv error');
assert.equal(flat.age, 0.5);
assert.equal(flat.fps, 30.5);
assert.equal(flat.session, 42);
assert.equal(flat.running, true);
const grouped = S.udpCounters({missing: {tx: 2, rx: 3}, late: [1, 2], invalid: 4,
  peer: {ip: '10.0.0.2', port: 9000}, fps: null});
assert.equal(grouped.missing, 5);
assert.equal(grouped.late, 3);
assert.equal(grouped.invalid, 4);
assert.equal(grouped.peer, '10.0.0.2:9000');
assert.equal(grouped.fps, null);

/* ---------- signals.js: detail-mode gap marks + duplex pairing ---------- */
const gapPoints = [{t: 0, segment: 0}, {t: 0.005, segment: 0}, {t: 0.02, segment: 0},
  {t: 0.25, segment: 0}, {t: 0.26, segment: 1}, {t: 0.40, segment: 1}];
const marks = S.gapMarks(gapPoints, 100);
assert.equal(marks.length, 3);
assert.equal(marks[0].kind, 'gap');
assert.ok(Math.abs(marks[0].ms - 230) < 1e-6);
assert.equal(marks[0].t, 0.25);
assert.equal(marks[1].kind, 'segment');
assert.equal(marks[1].t, 0.26);
assert.equal(marks[2].kind, 'gap');
assert.ok(Math.abs(marks[2].ms - 140) < 1e-6);
assert.deepEqual(S.gapMarks([{t: 0, segment: 0}, {t: 0.01, segment: 0}], 100), []);
assert.deepEqual(S.gapMarks([], 100), []);
assert.deepEqual(S.gapMarks(null), []);
assert.equal(S.gapMarks([{t: 0, segment: 0}, {t: 0.2, segment: 0}], 200).length, 0, 'threshold is strict >');
const tx = [{t: 0}, {t: 0.01}, {t: 0.02}, {t: 0.05}];
const rx = [{t: 0.012}, {t: 0.019}, {t: 0.06}, {t: 0.20}];
const pairs = S.duplexPairs(tx, rx, 100);
assert.equal(pairs.length, 3, 'RX 0.20 is older than 100 ms from the latest TX and must be skipped');
assert.equal(pairs[0].tx.t, 0.01);
assert.equal(pairs[0].rx.t, 0.012);
assert.ok(Math.abs(pairs[0].age - 2) < 1e-6);
assert.equal(pairs[1].rx.t, 0.019);
assert.ok(Math.abs(pairs[1].age - 9) < 1e-6);
assert.equal(pairs[2].tx.t, 0.05);
assert.ok(Math.abs(pairs[2].age - 10) < 1e-6);
assert.deepEqual(S.duplexPairs([], rx), []);
assert.deepEqual(S.duplexPairs([{t: 1.0}], [{t: 0.001}]), [], 'RX before first TX is not paired');
const pairAll = S.duplexPairs([{t: 0}], [{t: 0}, {t: 0.001}], 100);
assert.equal(pairAll.length, 2, 'duplicate t chooses the latest preceding TX');

/* ---------- native.js: fake DOM + routed fetch ---------- */
function classListOf() {
  const set = new Set();
  return {
    toggle(name, force) {
      const on = force === undefined ? !set.has(name) : !!force;
      if (on) set.add(name); else set.delete(name);
      return on;
    },
    add(name) { set.add(name); },
    remove(name) { set.delete(name); },
    contains(name) { return set.has(name); }
  };
}
class FakeElement {
  constructor(id) {
    this.id = id;
    this.textContent = '';
    this.title = '';
    this.hidden = id === 'native-tools' || id === 'desktop-badge';
    this.disabled = false;
    this.classList = classListOf();
    this._handlers = {};
  }
  addEventListener(type, callback) { (this._handlers[type] = this._handlers[type] || []).push(callback); }
  click() {
    const handlers = this._handlers.click || [];
    return Promise.all(handlers.map(callback => callback({ target: this, currentTarget: this, preventDefault() {} })));
  }
}
function immediate() { return new Promise(resolve => setImmediate(resolve)); }

(async () => {
  const ids = ['native-tools', 'native-save', 'native-record', 'native-decode', 'native-open',
    'native-status', 'desktop-badge'];
  const elements = new Map(ids.map(id => [id, new FakeElement(id)]));
  // Real index.html supplies these initial labels.
  elements.get('native-save').textContent = '保存 .bin';
  elements.get('native-record').textContent = '开始录制';
  elements.get('native-decode').textContent = '离线解码';
  elements.get('native-open').textContent = '打开输出目录';
  const body = { classList: classListOf() };
  const document = {
    title: '', body, hidden: false,
    getElementById: id => elements.get(id) || new FakeElement(id)
  };
  const calls = [];
  const respond = (status, payload) => ({ ok: status < 400, status, json: async () => payload });
  let statusData = { available: true, recording: null, last_decode: null, mode: 'live_udp',
    capabilities: ['save', 'record', 'decode', 'open'] };
  const results = {
    save: { status: 200, payload: {} },
    record: { status: 200, payload: {} },
    decode: { status: 200, payload: {} },
    open: { status: 200, payload: { ok: true } }
  };
  let operationGate = null;
  const fetch = async (url, options) => {
    const requestBody = options && options.body ? JSON.parse(options.body) : null;
    calls.push({ url, method: (options && options.method) || 'GET', body: requestBody });
    if (operationGate) { const gate = operationGate; await gate; }
    if (url === '/api/native/status') return respond(200, statusData);
    const key = url.split('/')[3];
    const result = results[key] || { status: 200, payload: {} };
    return respond(result.status, result.payload);
  };
  const timers = [];
  const window = {};
  const context = {
    window, document, fetch, console,
    AbortSignal, AbortController,
    setTimeout: callback => { timers.push(callback); return timers.length; },
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {}
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'web', 'native.js'), 'utf8'), context);
  await immediate();
  await immediate();

  const tools = elements.get('native-tools');
  const badge = elements.get('desktop-badge');
  const status = elements.get('native-status');
  const save = elements.get('native-save');
  const record = elements.get('native-record');
  const decode = elements.get('native-decode');
  const open = elements.get('native-open');
  const native = window.ServalNative;
  const lastCallTo = url => {
    const matching = calls.filter(call => call.url === url);
    return matching[matching.length - 1];
  };

  // 1. Native host available: initial status shows tools, badge, label and title.
  assert.equal(native.isAvailable(), true);
  assert.equal(tools.hidden, false, 'native tools visible when available');
  assert.equal(badge.hidden, false, 'desktop badge visible in native mode');
  assert.equal(record.textContent, '开始录制');
  assert.equal(open.disabled, true, 'open output dir disabled before any decode');
  assert.equal(document.title.includes('桌面 GUI（无浏览器）'), true, 'title marks desktop no-browser mode');
  assert.equal(body.classList.contains('native-desktop'), true);

  // 2. Recording state pushed through SSE snapshots flips the label.
  native.onSnapshot({ recording: { path: '/tmp/run.bin', active: true, written: 5, finished: false } });
  assert.equal(record.textContent, '停止录制');
  assert.equal(native.debug().recording, true, 'debug hook reports recording state');
  assert.equal(native.debug().available, true);
  native.onSnapshot({ recording: { path: '/tmp/run.bin', active: false, written: 9, finished: true } });
  assert.equal(record.textContent, '开始录制');
  native.onSnapshot({ recording: { path: '/tmp/run.bin', active: true, error: '磁盘已满' } });
  assert.equal(status.classList.contains('native-error'), true);
  native.onSnapshot({ recording: null });
  // UDP 会话切换时 backend 自动结束录制；原因经可选的 data.udp.recording_stopped_reason 提示一次。
  native.onSnapshot({ recording: null, udp: { recording_stopped_reason: 'UDP 会话切换（发送端可能重启），录制已自动结束' } });
  assert.match(status.textContent, /会话切换/);
  assert.equal(status.classList.contains('native-error'), false);

  // 3. Save .bin: POST {}, success text carries count + path.
  results.save = { status: 200, payload: { path: '/tmp/monitor.bin', count: 12 } };
  await save.click();
  await immediate();
  assert.equal(lastCallTo('/api/native/save').url, '/api/native/save');
  assert.deepEqual(lastCallTo('/api/native/save').body, {});
  assert.match(status.textContent, /已保存 12 包/);
  assert.equal(status.classList.contains('native-ok'), true);

  // 4. Cancel is quiet (no red error); error response is red.
  results.save = { status: 200, payload: { cancelled: true } };
  await save.click();
  await immediate();
  assert.equal(status.textContent, '已取消保存');
  assert.equal(status.classList.contains('native-error'), false);
  results.save = { status: 409, payload: { error: '没有可保存的数据' } };
  await save.click();
  await immediate();
  assert.match(status.textContent, /保存 .bin 失败：没有可保存的数据/);
  assert.equal(status.classList.contains('native-error'), true);

  // 5. Recording toggle: start and stop hit the right endpoint; cancel handled.
  results.record = { status: 200, payload: { recording: { path: '/r1.bin', active: true, written: 0 } } };
  statusData = { ...statusData, recording: results.record.payload.recording };
  await record.click();
  await immediate();
  assert.equal(lastCallTo('/api/native/record/start').url, '/api/native/record/start');
  assert.equal(record.textContent, '停止录制');
  assert.match(status.textContent, /录制已开始/);
  results.record = { status: 200, payload: { recording: { path: '/r1.bin', active: false, finished: true, written: 3 } } };
  statusData = { ...statusData, recording: null };
  await record.click();
  await immediate();
  assert.equal(lastCallTo('/api/native/record/stop').url, '/api/native/record/stop');
  assert.equal(record.textContent, '开始录制');
  results.record = { status: 200, payload: { cancelled: true } };
  await record.click();
  await immediate();
  assert.equal(status.textContent, '已取消录制');

  // 6. Decode offline: body {path:null,plot:true}, success enables "打开输出目录".
  results.decode = { status: 200, payload: { path: '/in.bin', output_dir: '/out/decode',
    plot: true, returncode: 0, output: '', error: '' } };
  statusData = { ...statusData, last_decode: results.decode.payload };
  await decode.click();
  await immediate();
  assert.equal(lastCallTo('/api/native/decode').url, '/api/native/decode');
  assert.deepEqual(lastCallTo('/api/native/decode').body, { path: null, plot: true });
  assert.match(status.textContent, /离线解码完成/);
  assert.match(status.textContent, /\/out\/decode/);
  assert.equal(open.disabled, false);

  // 7. Open output directory posts the decoded output path.
  await open.click();
  await immediate();
  assert.equal(lastCallTo('/api/native/open').url, '/api/native/open');
  assert.deepEqual(lastCallTo('/api/native/open').body, { path: '/out/decode' });
  assert.match(status.textContent, /已请求系统打开输出目录/);

  // 8. Decode failure keeps the red error.
  results.decode = { status: 200, payload: { path: '/in.bin', output_dir: null,
    plot: true, returncode: 2, output: 'traceback', error: '日志格式不符' } };
  statusData = { ...statusData, last_decode: results.decode.payload };
  await decode.click();
  await immediate();
  assert.match(status.textContent, /离线解码失败：日志格式不符/);
  assert.equal(status.classList.contains('native-error'), true);

  // 9. Busy guard: buttons disabled during a pending operation, labels restored.
  results.save = { status: 200, payload: { path: '/slow.bin', count: 1 } };
  let release;
  operationGate = new Promise(resolve => { release = resolve; });
  const slow = save.click();
  await immediate();
  assert.equal(save.disabled, true, 'save disabled while busy');
  assert.equal(record.disabled, true, 'record disabled while busy');
  assert.equal(save.textContent, '保存中…');
  release();
  await slow;
  operationGate = null;
  await immediate();
  assert.equal(save.disabled, false, 'save re-enabled after operation');
  assert.equal(save.textContent, '保存 .bin', 'busy label restored');

  // 10. Browser mode after a status refresh: hide everything again.
  statusData = { available: false, recording: null, last_decode: null, mode: 'idle', capabilities: [] };
  await native.refresh();
  assert.equal(tools.hidden, true);
  assert.equal(badge.hidden, true);
  assert.equal(body.classList.contains('native-desktop'), false);

  console.log('PASS: SSE snapshot parsing, UDP counters, native status/buttons/recording/decode/open, cancel, error and busy states');
})().catch(error => { console.error(error); process.exitCode = 1; });
