'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

(async () => {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, {
      textContent: '', classList: {toggle() {}, add() {}},
      addEventListener(type, callback) { this[type] = callback; }
    });
    return elements.get(id);
  };
  let sample = {mode:'idle',latest:{fps:{fields:{program:'standard_mpc',fps:123.45},age_s:0.2}},
    wire:{},history:[],explanation:{title:'Waiting',detail:'',code:'waiting'}};
  let nextPoll, disconnected = false;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'web', 'diagnostics.js'), 'utf8'), {
    document:{getElementById:get}, AbortSignal,
    fetch:async () => { if (disconnected) throw new Error('offline'); return {ok:true,json:async()=>sample}; },
    setTimeout:callback=>{nextPoll=callback;}
  });
  await new Promise(setImmediate);
  assert.equal(get('vision-fps').textContent, '123.45');
  assert.match(get('vision-fps-detail').textContent, /standard_mpc/);
  get('diagnostic-pause').click();
  sample.latest.fps.fields.fps = 0;
  await nextPoll();
  assert.equal(get('vision-fps').textContent, '0.00', 'pausing diagnostics does not freeze live FPS');
  sample.latest.fps.age_s = 4;
  await nextPoll();
  assert.equal(get('vision-fps').textContent, '—');
  assert.match(get('vision-fps-detail').textContent, /过期/);
  sample.latest.fps.age_s = 0;
  sample.mode = 'replay';
  await nextPoll();
  assert.equal(get('vision-fps').textContent, '—');
  assert.match(get('vision-fps-detail').textContent, /回放/);
  sample.mode = 'idle';
  sample.latest = {};
  await nextPoll();
  assert.match(get('vision-fps-detail').textContent, /等待/);
  disconnected = true;
  await nextPoll();
  assert.equal(get('vision-fps').textContent, '—');
  assert.match(get('vision-fps-detail').textContent, /未连接/);
  console.log('PASS: actual FPS, zero, pause, stale, replay, missing data and disconnect');
})().catch(error => { console.error(error); process.exitCode = 1; });
