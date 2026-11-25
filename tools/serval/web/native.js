'use strict';
/* 本地工具（仅 QtWebEngine 原生窗口内可用）。
   页面通过 /api/native/* 调用桌面窗口弹原生文件对话框；浏览器模式下整组隐藏。
   兼容 Chromium 83（QtWebEngine 5.15）：不用 AbortSignal.timeout / replaceChildren。 */
(() => {
  const get = id => document.getElementById(id);
  const tools = get('native-tools');
  if (!tools) return;
  const buttons = {
    save: get('native-save'),
    record: get('native-record'),
    decode: get('native-decode'),
    open: get('native-open')
  };
  const status = get('native-status');
  const badge = get('desktop-badge');
  const allButtons = [buttons.save, buttons.record, buttons.decode, buttons.open].filter(Boolean);
  const state = {
    available: false,
    busy: false,
    capabilities: [],
    recording: null,
    recordingError: '',
    recordingStoppedReason: '',
    lastOutputDir: null,
    statusTimer: null
  };
  const timeoutSignal = ms => {
    if (typeof AbortSignal !== 'undefined' && typeof AbortSignal.timeout === 'function') return AbortSignal.timeout(ms);
    const controller = new AbortController();
    setTimeout(() => controller.abort(), ms);
    return controller.signal;
  };
  function setStatus(text, tone) {
    if (!status) return;
    clearTimeout(state.statusTimer);
    status.textContent = text || '';
    status.classList.toggle('native-ok', tone === 'ok');
    status.classList.toggle('native-error', tone === 'error');
    status.title = text || '';
    if (text && tone !== 'error') {
      // 成功信息自动淡出；错误保留到下一次操作。
      state.statusTimer = setTimeout(() => {
        if (status.textContent === text) setStatus('');
      }, 12000);
    }
  }
  function recordingActive(recording) {
    const value = recording === undefined ? state.recording : recording;
    return !!value && value.active !== false && !value.finished;
  }
  function allowed(name) {
    return !state.capabilities.length || state.capabilities.indexOf(name) >= 0;
  }
  function syncButtons() {
    for (const name of ['save', 'record', 'decode', 'open']) {
      const button = buttons[name];
      if (!button) continue;
      let disabled = state.busy || !state.available || !allowed(name);
      if (name === 'open' && !state.lastOutputDir) disabled = true;
      button.disabled = disabled;
    }
    if (buttons.record && !state.busy) buttons.record.textContent = recordingActive() ? '停止录制' : '开始录制';
  }
  function updateRecording(recording) {
    state.recording = recording || null;
    if (buttons.record && !state.busy) buttons.record.textContent = recordingActive() ? '停止录制' : '开始录制';
    const error = (state.recording && state.recording.error) || '';
    if (error && error !== state.recordingError) setStatus('录制写入错误：' + error, 'error');
    state.recordingError = error;
  }
  function useDecode(result) {
    if (!result || typeof result !== 'object') return;
    if (result.output_dir) state.lastOutputDir = String(result.output_dir);
  }
  function applyStatus(data) {
    state.available = !!(data && data.available);
    tools.hidden = !state.available;
    if (badge) badge.hidden = !state.available;
    if (document.body) document.body.classList.toggle('native-desktop', state.available);
    // 桌面窗口标记：gui_smoke.py 用它判断页面确实运行在原生窗口内。
    window.ServalDesktop = { mode: 'desktop-webengine', native: state.available,
      browser: !state.available, transport: 'sse' };
    if (!state.available) return;
    document.title = 'Serval Scope · 桌面 GUI（无浏览器）';
    if (Array.isArray(data.capabilities)) {
      state.capabilities = data.capabilities.filter(item => typeof item === 'string');
    }
    if (Object.prototype.hasOwnProperty.call(data, 'recording')) updateRecording(data.recording);
    useDecode(data.last_decode);
    syncButtons();
  }
  async function refreshStatus() {
    try {
      const response = await fetch('/api/native/status', { signal: timeoutSignal(4000), cache: 'no-store' });
      if (!response.ok) throw new Error('HTTP ' + response.status);
      applyStatus(await response.json());
    } catch (error) {
      if (state.available) setStatus('本地工具服务不可连接：' + error.message, 'error');
    }
  }
  async function postNative(path, payload) {
    const response = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload || {})
    });
    let result = null;
    try { result = await response.json(); } catch (error) { result = null; }
    if (!response.ok) throw new Error((result && result.error) ? result.error : 'HTTP ' + response.status);
    return (result && typeof result === 'object') ? result : {};
  }
  async function runOperation(button, busyText, operation) {
    if (state.busy) return;
    state.busy = true;
    const previous = button ? button.textContent : '';
    if (button) button.textContent = busyText;
    syncButtons();
    try {
      await operation();
    } finally {
      state.busy = false;
      if (button) button.textContent = previous;
      syncButtons();
    }
  }
  if (buttons.save) buttons.save.addEventListener('click', () => runOperation(buttons.save, '保存中…', async () => {
    setStatus('正在打开保存对话框，选择 .bin 保存位置…');
    try {
      const result = await postNative('/api/native/save', {});
      if (result.cancelled) { setStatus('已取消保存'); return; }
      const count = Number.isFinite(result.count) ? result.count.toLocaleString() : null;
      if (result.path) setStatus('已保存' + (count === null ? '' : ' ' + count + ' 包') + ' · ' + result.path, 'ok');
      else setStatus('已保存' + (count === null ? '' : ' ' + count + ' 包'), 'ok');
    } catch (error) {
      setStatus('保存 .bin 失败：' + error.message, 'error');
    } finally {
      refreshStatus();
    }
  }));
  if (buttons.record) buttons.record.addEventListener('click', () => {
    const wasActive = recordingActive();
    return runOperation(buttons.record, wasActive ? '停止中…' : '准备录制…', async () => {
      setStatus(wasActive ? '正在停止录制并保存原始记录…' : '请选择录制文件（原始字节逐字节保存）…');
      try {
        const result = await postNative(wasActive ? '/api/native/record/stop' : '/api/native/record/start', {});
        if (result.cancelled) { setStatus('已取消录制'); return; }
        if (Object.prototype.hasOwnProperty.call(result, 'recording')) updateRecording(result.recording);
        setStatus(wasActive ? '录制已停止，原始 .bin 已保存' : '录制已开始，原始 .bin 逐字节保存', 'ok');
      } catch (error) {
        setStatus((wasActive ? '停止录制失败：' : '开始录制失败：') + error.message, 'error');
      } finally {
        refreshStatus();
      }
    });
  });
  if (buttons.decode) buttons.decode.addEventListener('click', () => runOperation(buttons.decode, '解算中…', async () => {
    setStatus('请选择 .bin 日志，完成后可打开输出目录…');
    try {
      const result = await postNative('/api/native/decode', { path: null, plot: true });
      if (result.cancelled) { setStatus('已取消离线解码'); return; }
      useDecode(result);
      syncButtons();
      const output = result.output_dir ? ' · 输出目录 ' + result.output_dir : '';
      if (result.error) {
        setStatus('离线解码失败：' + result.error + output, 'error');
        return;
      }
      setStatus('离线解码完成' + (result.plot ? '（含 waveform.png）' : '') + output, 'ok');
    } catch (error) {
      setStatus('离线解码失败：' + error.message, 'error');
    } finally {
      refreshStatus();
    }
  }));
  if (buttons.open) buttons.open.addEventListener('click', () => {
    if (!state.lastOutputDir) return;
    return runOperation(buttons.open, '打开中…', async () => {
      try {
        const result = await postNative('/api/native/open', { path: state.lastOutputDir });
        if (result.ok === false || result.error) throw new Error(result.error || '系统未能打开目录');
        setStatus('已请求系统打开输出目录：' + state.lastOutputDir, 'ok');
      } catch (error) {
        setStatus('打开输出目录失败：' + error.message, 'error');
      }
    });
  });
  // app.js 在每份数据快照后回调，录制状态随 SSE 实时刷新（另有低频 status 兜底）。
  window.ServalNative = {
    refresh: refreshStatus,
    isAvailable: () => state.available,
    debug: () => ({
      available: state.available, busy: state.busy, recording: recordingActive(),
      capabilities: state.capabilities.slice(), lastOutputDir: state.lastOutputDir,
      status: status ? status.textContent : ''
    }),
    onSnapshot: data => {
      if (!state.available || !data || state.busy) return;
      if (Object.prototype.hasOwnProperty.call(data, 'recording')) updateRecording(data.recording);
      // UDP 会话切换会在后端自动结束录制；把原因顺手提示一次。
      const udp = data.udp;
      const reason = udp && typeof udp.recording_stopped_reason === 'string' ? udp.recording_stopped_reason : '';
      if (reason) {
        if (reason !== state.recordingStoppedReason) {
          state.recordingStoppedReason = reason;
          setStatus(reason);
        }
      } else {
        state.recordingStoppedReason = '';
      }
    }
  };
  if (allButtons.length && typeof setInterval === 'function') {
    setInterval(() => { if (state.available) refreshStatus(); }, 3000);
  }
  refreshStatus();
})();
