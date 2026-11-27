'use strict';
/* Serval 极简波形页：只绘制 Yaw/Pitch 的 TX（下发）与 RX（云台回传）比对。
   数据经 SSE 增量推送；页面只保留两条波形 + 关键读数，减少 Jetson/Windows 渲染负担。
   Chromium 83（QtWebEngine 5.15）兼容：不使用 AbortSignal.timeout / replaceChildren / .at 等新 API。 */
const $ = id => document.getElementById(id);
const TX_COLOR = '#61b7ff';
const RX_COLOR = '#38d9b6';
const WINDOW_KEY = 'serval.lite.window';
const CAP_DEFAULT = 60000;

const timeoutSignal = ms => {
  if (typeof AbortSignal !== 'undefined' && typeof AbortSignal.timeout === 'function') {
    return AbortSignal.timeout(ms);
  }
  const controller = new AbortController();
  setTimeout(() => controller.abort(), ms);
  return controller.signal;
};

let frames = [];
let cursor = 0;
let epoch = null;
let lastAppliedSeq = 0;
let serverNow = 0;
let lastPoll = performance.now();
let lastData = null;
let paused = false;
let pausedFrames = null;
let pausedNow = 0;
let windowSec = 10;
let detailMode = false;
let cutoff = null;
let drawQueued = false;
let lastUiAt = 0;
let lastPushAt = null;
let pushEvents = 0;
let drawCount = 0;
let drawWindowStart = performance.now();
let drawFps = 0;
let stream = null;
let streamConnected = false;
let pollTimer = null;
let retryTimer = null;
let capacity = CAP_DEFAULT;

const CHARTS = [
  { key: 'yaw', title: 'Yaw 偏航角', unit: '°', el: null, canvas: null, ctx: null },
  { key: 'pitch', title: 'Pitch 俯仰角', unit: '°', el: null, canvas: null, ctx: null }
];

function fmt(n, digits = 3) {
  return Number.isFinite(n) ? n.toFixed(digits) : '—';
}

function deg(rad) {
  return Number.isFinite(rad) ? rad * 180 / Math.PI : NaN;
}

function setText(id, text) {
  const el = $(id);
  if (el) el.textContent = text;
}

/* 发送端时钟：帧 t 是发送端记录时间，不能拿本机时钟当横轴。 */
function snapshotNow(data) {
  if (data && data.mode === 'live_udp' && data.udp) {
    const latest = Math.max(
      data.latest && Number.isFinite(data.latest.rx && data.latest.rx.t) ? data.latest.rx.t : -Infinity,
      data.latest && Number.isFinite(data.latest.tx && data.latest.tx.t) ? data.latest.tx.t : -Infinity);
    if (Number.isFinite(latest) && Number.isFinite(data.udp.age)) return latest + data.udp.age;
  }
  return data ? data.now : serverNow;
}

function rangeEnd() {
  if (paused) return pausedNow;
  return snapshotNow(lastData) + (lastData && lastData.mode === 'replay' ? 0 : (performance.now() - lastPoll) / 1000);
}

function createCharts() {
  const host = $('charts');
  for (const chart of CHARTS) {
    const box = document.createElement('section');
    box.className = 'chart';
    box.innerHTML = '<div class="chart-head"><h3></h3><span class="legend">' +
      '<i class="swatch tx"></i>实际下发 <i class="swatch rx"></i>实际回传</span></div><canvas></canvas>';
    box.querySelector('h3').textContent = chart.title;
    host.appendChild(box);
    chart.el = box;
    chart.canvas = box.querySelector('canvas');
    chart.ctx = chart.canvas.getContext('2d');
  }
}

function makePoints(source, direction, key) {
  const out = [];
  let prev = null;
  for (const frame of source) {
    const values = frame.values;
    if (!values || frame.direction !== direction) continue;
    const raw = values[key];
    if (!Number.isFinite(raw)) continue;
    out.push({ t: frame.t, v: deg(raw), mode: values.mode });
  }
  out.sort((a, b) => a.t - b.t);
  return out;
}

/* 逐像素 min/max 包络只用于绘制；数据/统计仍是原始样本。 */
function envelope(points, left, right, xOf) {
  if (points.length < 4 || right - left < 8) return points;
  const buckets = new Map();
  for (const p of points) {
    const bucket = Math.floor(xOf(p.t));
    const item = buckets.get(bucket);
    if (!item) {
      buckets.set(bucket, { first: p, last: p, min: p, max: p });
      continue;
    }
    item.last = p;
    if (p.v < item.min.v) item.min = p;
    if (p.v > item.max.v) item.max = p;
  }
  const result = [];
  const keys = Array.from(buckets.keys()).sort((a, b) => a - b);
  for (const key of keys) {
    const item = buckets.get(key);
    const chosen = [item.first, item.min, item.max, item.last];
    for (const p of chosen) {
      if (!result.length || result[result.length - 1] !== p) result.push(p);
    }
  }
  return result;
}

function drawChart(chart, end) {
  const canvas = chart.canvas;
  if (!canvas) return;
  const rect = canvas.parentElement.getBoundingClientRect();
  const view = paused && pausedFrames ? pausedFrames : frames;
  const start = end - windowSec;
  const source = view.filter(f => f.t >= start - 1 && f.t <= end + 1);
  const tx = makePoints(source, 'tx', chart.key);
  const rx = makePoints(source, 'rx', chart.key);
  if (!tx.length && !rx.length) {
    const ctx = chart.ctx;
    const ratio = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.floor(rect.width));
    const h = Math.max(1, Math.floor(rect.height));
    if (canvas.width !== Math.round(w * ratio) || canvas.height !== Math.round(h * ratio)) {
      canvas.width = Math.round(w * ratio);
      canvas.height = Math.round(h * ratio);
    }
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = '#8294ac';
    ctx.font = '12px Consolas, monospace';
    ctx.textAlign = 'center';
    ctx.fillText('等待数据', w / 2, h / 2);
    return;
  }
  const ratio = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.floor(rect.width));
  const h = Math.max(1, Math.floor(rect.height));
  if (canvas.width !== Math.round(w * ratio) || canvas.height !== Math.round(h * ratio)) {
    canvas.width = Math.round(w * ratio);
    canvas.height = Math.round(h * ratio);
  }
  const ctx = chart.ctx;
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const left = 58, right = w - 16, top = 14, bottom = h - 26;
  if (right <= left || bottom <= top) return;
  let lo = Infinity, hi = -Infinity;
  for (const p of tx) { lo = Math.min(lo, p.v); hi = Math.max(hi, p.v); }
  for (const p of rx) { lo = Math.min(lo, p.v); hi = Math.max(hi, p.v); }
  if (!Number.isFinite(lo)) { lo = -1; hi = 1; }
  const pad = Math.max((hi - lo) * 0.12, 0.3);
  lo -= pad; hi += pad;
  const x = t => left + (t - start) / windowSec * (right - left);
  const y = v => bottom - (v - lo) / (hi - lo) * (bottom - top);

  ctx.font = '10px Consolas, monospace';
  for (let i = 0; i < 5; i++) {
    const yy = top + (bottom - top) * i / 4;
    ctx.strokeStyle = '#243248';
    ctx.beginPath(); ctx.moveTo(left, yy); ctx.lineTo(right, yy); ctx.stroke();
    ctx.fillStyle = '#7f93af'; ctx.textAlign = 'right';
    ctx.fillText((hi - (hi - lo) * i / 4).toFixed(2), left - 8, yy + 3);
  }
  for (let i = 0; i < 5; i++) {
    const tt = start + windowSec * i / 4;
    const xx = x(tt);
    ctx.strokeStyle = '#1d2a3d';
    ctx.beginPath(); ctx.moveTo(xx, top); ctx.lineTo(xx, bottom); ctx.stroke();
    ctx.fillStyle = '#7f93af'; ctx.textAlign = 'center';
    ctx.fillText(tt.toFixed(windowSec <= 5 ? 2 : 1) + 's', xx, bottom + 16);
  }

  ctx.save();
  ctx.beginPath(); ctx.rect(left, top, right - left, bottom - top); ctx.clip();
  const drawSeries = (points, color, step) => {
    if (!points.length) return;
    const list = detailMode ? points : envelope(points, left, right, x);
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    let prev = null;
    for (const p of list) {
      const xx = x(p.t), yy = y(p.v);
      if (!prev || p.t - prev.t > .1) {
        ctx.moveTo(xx, yy);
      } else if (step) {
        ctx.lineTo(xx, y(prev.v));
        ctx.lineTo(xx, yy);
      } else {
        // 角度回绕/模式切换处断开，避免一条竖直假跃迁。
        if (Math.abs(p.v - prev.v) > 180) ctx.moveTo(xx, yy); else ctx.lineTo(xx, yy);
      }
      prev = p;
    }
    ctx.stroke();
    if (detailMode && list.length <= 4000) {
      ctx.fillStyle = color;
      for (const p of list) { ctx.beginPath(); ctx.arc(x(p.t), y(p.v), 1.6, 0, Math.PI * 2); ctx.fill(); }
    }
  };
  drawSeries(tx, TX_COLOR, true);
  drawSeries(rx, RX_COLOR, false);
  ctx.restore();
}

function drawAll() {
  if (!CHARTS[0] || !CHARTS[0].canvas) return;
  if (document.hidden) return;
  const end = rangeEnd();
  for (const chart of CHARTS) drawChart(chart, end);
  drawCount++;
  const now = performance.now();
  if (now - drawWindowStart >= 1000) {
    drawFps = drawCount * 1000 / (now - drawWindowStart);
    drawCount = 0; drawWindowStart = now;
  }
  updateUi(end, now);
}

function wrapDeg(v) {
  let x = (v + 180) % 360;
  if (x < 0) x += 360;
  return x - 180;
}

function updateUi(end, now) {
  if (now - lastUiAt < 100) return;
  lastUiAt = now;
  const data = lastData;
  const latestTx = data && data.latest && data.latest.tx;
  const latestRx = data && data.latest && data.latest.rx;
  const txYaw = latestTx && latestTx.values ? deg(latestTx.values.yaw) : NaN;
  const txPitch = latestTx && latestTx.values ? deg(latestTx.values.pitch) : NaN;
  const rxYaw = latestRx && latestRx.values ? deg(latestRx.values.yaw) : NaN;
  const rxPitch = latestRx && latestRx.values ? deg(latestRx.values.pitch) : NaN;
  setText('tx-yaw', txtDeg(txYaw));
  setText('tx-pitch', txtDeg(txPitch));
  setText('rx-yaw', txtDeg(rxYaw));
  setText('rx-pitch', txtDeg(rxPitch));
  setText('err-yaw', Number.isFinite(txYaw) && Number.isFinite(rxYaw) ? fmt(wrapDeg(txYaw - rxYaw), 3) + '°' : '—');
  setText('err-pitch', Number.isFinite(txPitch) && Number.isFinite(rxPitch) ? fmt(txPitch - rxPitch, 3) + '°' : '—');
  setText('yaw-value', txtDeg(txYaw));
  setText('pitch-value', txtDeg(txPitch));
  setText('draw-fps', drawFps.toFixed(0));
  setText('retained', String((data && data.retained) || frames.length));
  const udp = (data && data.udp) || {};
  setText('udp-missing', String(udp.missing || 0));
  setText('udp-late', String(udp.late || 0));
  setText('udp-invalid', String(udp.invalid || 0));
  let modeText = '—';
  let statusText = '等待数据';
  let sourceText = '';
  if (data) {
    if (data.mode === 'live_udp') {
      modeText = 'UDP 二进制';
      statusText = udp.age !== null && udp.age !== undefined && udp.age > 2 ? 'UDP 已中断' : 'UDP 实时接收中';
      sourceText = udp.peer ? '· UDP ' + udp.peer : '· UDP';
    } else if (data.mode === 'demo') {
      modeText = '演示数据'; statusText = '演示运行中'; sourceText = '· 演示数据';
    } else if (data.mode === 'replay') {
      modeText = '离线回放';
      statusText = data.replay && data.replay.playing ? '回放中' : '回放已暂停';
      sourceText = data.source ? '· ' + data.source : '';
    } else {
      modeText = data.mode || '—';
      statusText = data.retained ? '数据接收中' : '等待通信数据';
      sourceText = data.source ? '· ' + data.source : '';
    }
  }
  setText('status', statusText);
  setText('source', sourceText);
  setText('source-mode', modeText);
  setText('tx-rate', String(Math.round((data && data.rate && data.rate.tx) || 0)));
  setText('rx-rate', String(Math.round((data && data.rate && data.rate.rx) || 0)));
  const dot = $('status-dot');
  if (dot) dot.className = data && data.mode === 'live_udp' ? (udp.age !== null && udp.age > 2 ? 'bad' : 'good') : '';
  try { if (window.ServalNative && window.ServalNative.onSnapshot) window.ServalNative.onSnapshot(data); } catch (error) { /* ignore */ }
}

function txtDeg(v) {
  return Number.isFinite(v) ? fmt(v, 3) + '°' : '—';
}

function requestDraw() {
  if (drawQueued || document.hidden) return;
  drawQueued = true;
  const raf = window.requestAnimationFrame || (cb => setTimeout(cb, 16));
  raf(flushFrame);
}

function flushFrame() {
  drawQueued = false;
  drawAll();
}

function applyData(data) {
  if (epoch !== data.epoch) {
    frames = [];
    epoch = data.epoch;
    lastAppliedSeq = 0;
    cursor = 0;
    cutoff = null;
    paused = false;
    pausedFrames = null;
    updatePauseButton();
  }
  if (Number.isFinite(data.capacity)) capacity = Math.max(10000, Math.min(500000, data.capacity));
  const incoming = data.frames || [];
  const fresh = incoming.filter(frame => frame.seq > lastAppliedSeq);
  cursor = data.cursor;
  serverNow = data.now;
  lastPoll = performance.now();
  lastData = data;
  if (fresh.length) {
    let ordered = frames.length === 0 || frames[frames.length - 1].t <= fresh[0].t;
    if (ordered) {
      for (let i = 1; i < fresh.length; i++) {
        if (fresh[i].t < fresh[i - 1].t) { ordered = false; break; }
      }
    }
    for (const frame of fresh) frames.push(frame);
    if (!ordered) frames.sort((a, b) => a.t - b.t || a.seq - b.seq);
    if (frames.length > capacity) frames = frames.slice(frames.length - capacity);
    const limit = snapshotNow(data) - 130;
    if (frames.length && frames[0].t < limit) {
      let lo = 0, hi = frames.length;
      while (lo < hi) { const mid = (lo + hi) >> 1; if (frames[mid].t < limit) lo = mid + 1; else hi = mid; }
      if (lo > 0) frames = frames.slice(lo);
    }
  }
  lastAppliedSeq = data.cursor;
  requestDraw();
}

async function poll() {
  if (streamConnected) return;
  try {
    const response = await fetch('/api/data?since=' + cursor, { signal: timeoutSignal(4000) });
    if (!response.ok) throw new Error('HTTP ' + response.status);
    const data = await response.json();
    if (!streamConnected) applyData(data);
  } catch (error) {
    setText('status', '监听服务未连接');
    setText('source', '请确认 monitor.py 正在运行');
  }
  if (!streamConnected) pollTimer = setTimeout(poll, 100);
}

function startPoll() {
  if (pollTimer || streamConnected) return;
  setText('transport', 'HTTP 轮询（回退）');
  pollTimer = setTimeout(poll, 50);
}

function startStream() {
  if (typeof EventSource === 'undefined') { startPoll(); return; }
  if (stream) return;
  setText('transport', 'SSE 推送');
  try {
    stream = new EventSource('/api/stream?since=' + encodeURIComponent(cursor));
  } catch (error) {
    stream = null;
    startPoll();
    return;
  }
  const onSnapshot = event => {
    if (!event || typeof event.data !== 'string') return;
    if (!streamConnected) { streamConnected = true; setText('transport', 'SSE 推送'); }
    pushEvents++;
    lastPushAt = performance.now();
    let data = null;
    try { data = JSON.parse(event.data); } catch (error) { return; }
    applyData(data);
  };
  stream.onmessage = onSnapshot;
  stream.addEventListener('data', onSnapshot);
  stream.onerror = () => {
    if (stream) { stream.close(); stream = null; }
    streamConnected = false;
    startPoll();
    if (!retryTimer) {
      retryTimer = setTimeout(() => {
        retryTimer = null;
        if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
        startStream();
      }, 2000);
    }
  };
}

async function loadInitial() {
  try {
    const response = await fetch('/api/data?since=0', { signal: timeoutSignal(8000) });
    if (!response.ok) throw new Error('HTTP ' + response.status);
    applyData(await response.json());
  } catch (error) {
    setText('status', '监听服务未连接');
    setText('source', '请确认 monitor.py 正在运行');
  }
  startStream();
}

function updatePauseButton() {
  const button = $('pause');
  if (!button) return;
  button.textContent = paused ? '继续' : '暂停';
  button.classList.toggle('active', paused);
}

$('window').addEventListener('change', event => {
  windowSec = Number(event.target.value) || 10;
  try { localStorage.setItem(WINDOW_KEY, String(windowSec)); } catch (error) { /* ignore */ }
  requestDraw();
});
$('detail-mode').addEventListener('change', event => {
  detailMode = !!event.target.checked;
  requestDraw();
});
$('pause').addEventListener('click', () => {
  paused = !paused;
  if (paused) { pausedFrames = frames.slice(); pausedNow = rangeEnd(); }
  else { pausedFrames = null; }
  updatePauseButton();
  requestDraw();
});
$('clear').addEventListener('click', () => {
  frames = [];
  cutoff = rangeEnd();
  if (pausedFrames) pausedFrames = [];
  requestDraw();
});
window.addEventListener('pagehide', () => { if (stream) stream.close(); });
window.addEventListener('resize', () => requestDraw());

window.ServalDebug = () => ({
  frames: frames.length,
  cursor: cursor,
  serverNow: serverNow,
  displayNow: rangeEnd(),
  transport: streamConnected ? 'sse' : (pollTimer ? 'poll' : 'idle'),
  mode: lastData ? lastData.mode : null,
  epoch: epoch,
  retained: lastData ? lastData.retained : frames.length,
  latestT: lastData && lastData.latest
    ? Math.max((lastData.latest.tx && lastData.latest.tx.t) || -Infinity,
               (lastData.latest.rx && lastData.latest.rx.t) || -Infinity)
    : null,
  udp: lastData ? lastData.udp : null,
  drawFps: drawFps,
  pushEvents: pushEvents,
  detailMode: detailMode,
  paused: paused,
  charts: CHARTS.length,
  nativeToolsVisible: !!($('native-tools') && !$('native-tools').hidden),
  status: ($('status') && $('status').textContent) || ''
});

createCharts();
try {
  const saved = Number(localStorage.getItem(WINDOW_KEY));
  if (Number.isFinite(saved) && saved > 0) {
    windowSec = saved;
    $('window').value = String(saved);
    if (!document.querySelector('#window option[value="' + saved + '"]')) windowSec = 10;
  }
} catch (error) { /* ignore */ }
loadInitial();
