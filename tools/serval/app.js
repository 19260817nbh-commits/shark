'use strict';
const $ = id => document.getElementById(id);
const colors={tx:'#61b7ff',rx:'#38d9b6',imu:'#bc9bff',second:'#f4bb68',third:'#f27a91',fourth:'#38d9b6'};
let frames=[],cursor=0,serverNow=0,lastPoll=performance.now(),unit='deg',windowSec=10;
let freeze=null,cutoff=0,latest={},charts=[],sampleDrops=0,epoch=null,busy=false;
let viewEnd=null,hoverTime=null,lastProbeTime=null,markerA=null,markerB=null,drag=null,analysisVersion=0,channel='angle',fixedY=false;
const series=(direction,key,label,color=colors[direction])=>({direction,key,label,color,visible:true});
const chartDefs=[
 ['Yaw','偏航角','angle',[series('tx','yaw','实际下发'),series('rx','yaw','实际回传')]],
 ['Pitch','俯仰角','angle',[series('tx','pitch','实际下发'),series('rx','pitch','实际回传')]],
 ['Yaw error','偏航误差 · TX − RX','angle',[series('error','yaw','误差','#f4bb68')]],
 ['Pitch error','俯仰误差 · TX − RX','angle',[series('error','pitch','误差','#f4bb68')]],
 ['Yaw velocity','偏航角速度','rate',[series('tx','yaw_vel','实际下发'),series('rx','yaw_vel','实际回传')]],
 ['Pitch velocity','俯仰角速度','rate',[series('tx','pitch_vel','实际下发'),series('rx','pitch_vel','实际回传')]],
 ['Yaw acceleration','偏航角加速度 · 协议无加速度回传','accel',[series('tx','yaw_acc','实际下发')]],
 ['Pitch acceleration','俯仰角加速度 · 协议无加速度回传','accel',[series('tx','pitch_acc','实际下发')]]
];
const imuDefs=[
 ['IMU attitude','姿态欧拉角','angle',[series('rx','q_yaw','Yaw',colors.imu),series('rx','q_pitch','Pitch',colors.second),series('rx','q_roll','Roll',colors.third)]],
 ['Quaternion','四元数 · wxyz','scalar',[series('rx','qw','w',colors.imu),series('rx','qx','x',colors.second),series('rx','qy','y',colors.third),series('rx','qz','z',colors.fourth)]]
];
function scaleValue(value,kind){return kind!=='scalar'&&unit==='deg'?value*180/Math.PI:value}
function unitText(kind){return kind==='scalar'?'无量纲':(unit==='deg'?'°':'rad')+({angle:'',rate:'/s',accel:'/s²'}[kind])}
function fmt(n,digits=3){return Number.isFinite(n)?n.toFixed(digits):'—'}
function numberLabel(n){if(Math.abs(n)>=10000)return n.toExponential(1);return n.toFixed(Math.abs(n)<.1?4:Math.abs(n)<10?2:1)}
function rangeEnd(){return viewEnd??(freeze?freeze.now:serverNow+(lastData?.mode==='replay'?0:(performance.now()-lastPoll)/1000))}
function freezeNow(){if(!freeze){freeze={frames:frames.slice(),now:rangeEnd()};$('pause').textContent='▶ 继续曲线';$('pause').classList.add('active')}}
function canvasTime(chart,x){const w=chart.canvas.getBoundingClientRect().width;return rangeEnd()-windowSec+Math.max(0,Math.min(1,(x-65)/(w-85)))*windowSec}
function createChart(def,container){
 const [name,subtitle,kind,items]=def,box=document.createElement('section');box.className='chart';
 box.innerHTML='<div class="chart-head"><h3></h3><div class="legend"></div></div><div class="chart-value">等待数据</div><canvas></canvas><div class="chart-bottom"><span></span><span>原始样本 · 无平滑</span></div>';
 box.querySelector('h3').textContent=name;const small=document.createElement('small');small.textContent=subtitle;box.querySelector('h3').append(small);
 const chart={box,kind,items,canvas:box.querySelector('canvas')};
 for(const item of items){const label=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.checked=true;input.style.setProperty('--color',item.color);input.setAttribute('aria-label',name+' '+item.label);input.addEventListener('change',()=>{item.visible=input.checked;drawAll()});label.append(input,document.createTextNode(item.label));box.querySelector('.legend').append(label)}
 chart.canvas.setAttribute('aria-label',name+' 实时曲线');
 chart.canvas.addEventListener('pointermove',ev=>{
  hoverTime=canvasTime(chart,ev.offsetX);
  lastProbeTime=hoverTime;
  if(drag&&drag.chart===chart){const w=chart.canvas.getBoundingClientRect().width;viewEnd=drag.end-(ev.clientX-drag.x)/(w-85)*windowSec}
  drawAll();
 });
 chart.canvas.addEventListener('pointerleave',()=>{if(!drag){hoverTime=null;drawAll()}});
 chart.canvas.addEventListener('pointerdown',ev=>{if(ev.button!==0)return;freezeNow();drag={chart,x:ev.clientX,end:rangeEnd()};chart.canvas.setPointerCapture(ev.pointerId)});
 chart.canvas.addEventListener('pointerup',()=>{drag=null;requestAnalysis()});
 chart.canvas.addEventListener('wheel',ev=>{ev.preventDefault();freezeNow();const anchor=canvasTime(chart,ev.offsetX),fraction=(anchor-(rangeEnd()-windowSec))/windowSec;windowSec=Math.max(.05,Math.min(120,windowSec*Math.exp(ev.deltaY*.0015)));viewEnd=anchor+(1-fraction)*windowSec;drawAll();requestAnalysis()},{passive:false});
 chart.canvas.addEventListener('dblclick',()=>{$('reset-view').click()});
 container.append(box);charts.push(chart);
}
chartDefs.forEach((def,i)=>{createChart(def,$('charts'));const chart=charts[charts.length-1];chart.group=i===2||i===3?'error':def[2];chart.box.hidden=chart.group!==channel;});imuDefs.forEach(def=>createChart(def,$('imu-charts')));
function makeData(source){
 const S=window.ServalSignals,continuous=$('angle-mode').value==='continuous',cache=new Map();
 for(const chart of charts)for(const s of chart.items){const key=s.direction+':'+s.key;if(cache.has(key))continue;cache.set(key,s.direction==='error'?S.errorPoints(source,s.key):S.points(source,s.direction,s.key,continuous))}
 // Same circular branch for angle overlays; never alter stored raw values.
 if(continuous){const tx=cache.get('tx:yaw'),rx=cache.get('rx:yaw');if(tx?.length&&rx?.length){const ref=rx.find(p=>p.t>=tx[0].t),prior=ref?S.prior(tx,ref.t,1):null;if(prior){const shift=Math.round((prior.value-ref.value)/(2*Math.PI))*2*Math.PI;cache.set('rx:yaw',rx.map(p=>({...p,value:p.value+shift})))}}}
 return cache;
}
function drawChart(chart,cache,end){
 const {canvas,kind,items}=chart,rect=canvas.getBoundingClientRect();if(!rect.width||!rect.height)return;
 const ratio=window.devicePixelRatio||1,w=rect.width,h=rect.height;
 if(canvas.width!==Math.round(w*ratio)||canvas.height!==Math.round(h*ratio)){canvas.width=Math.round(w*ratio);canvas.height=Math.round(h*ratio)}
 const ctx=canvas.getContext('2d');ctx.setTransform(ratio,0,0,ratio,0,0);ctx.clearRect(0,0,w,h);
 const left=65,right=w-20,top=17,bottom=h-31,start=end-windowSec;
 let lo=Infinity,hi=-Infinity,total=0;
 const data=items.filter(s=>s.visible).map(s=>{
  const all=cache.get(s.direction+':'+s.key)||[],visible=all.filter(p=>p.t>=Math.max(cutoff,start)&&p.t<=end);
  // Include a single predecessor to draw a held command at the viewport edge.
  const prev=window.ServalSignals.prior(all,start);if(prev&&prev.t<start&&prev.t>=cutoff)visible.unshift(prev);
  const points=visible.map(p=>({...p,value:scaleValue(p.value,kind)}));
  for(const p of points){lo=Math.min(lo,p.value);hi=Math.max(hi,p.value)}total+=visible.length;
  return {s,all,points};
 });
 if(!Number.isFinite(lo)){lo=-1;hi=1}if(items[0].direction==='error'){lo=Math.min(0,lo);hi=Math.max(0,hi)}
 const pad=Math.max((hi-lo)*.15,kind==='scalar'?.01:(unit==='deg'?.02:.00035));lo-=pad;hi+=pad;
 if(fixedY&&chart.limits){[lo,hi]=chart.limits}else chart.limits=[lo,hi];
 const x=t=>left+(t-start)/windowSec*(right-left),y=v=>bottom-(v-lo)/(hi-lo)*(bottom-top);
 ctx.font='10px Consolas, monospace';ctx.lineWidth=1;
 for(let i=0;i<5;i++){const yy=top+(bottom-top)*i/4;ctx.strokeStyle='#243248';ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(right,yy);ctx.stroke();ctx.fillStyle='#7f93af';ctx.textAlign='right';ctx.fillText(numberLabel(hi-(hi-lo)*i/4),left-10,yy+3)}
 for(let i=0;i<5;i++){const t=start+windowSec*i/4,xx=x(t);ctx.strokeStyle='#1d2a3d';ctx.beginPath();ctx.moveTo(xx,top);ctx.lineTo(xx,bottom);ctx.stroke();ctx.fillStyle='#7f93af';ctx.textAlign='center';ctx.fillText(t.toFixed(windowSec<1?3:2)+'s',xx,bottom+19)}
 ctx.save();ctx.beginPath();ctx.rect(left,top,right-left,bottom-top);ctx.clip();
 for(const {s,points} of data){const path=window.ServalSignals.envelope(points,right-left,x);ctx.strokeStyle=s.color;ctx.lineWidth=1.7;ctx.beginPath();let prev=null;
  for(const p of path){if(!prev||p.segment!==prev.segment||p.t-prev.t>.1)ctx.moveTo(x(p.t),y(p.value));else if(s.direction==='tx'){ctx.lineTo(x(p.t),y(prev.value));ctx.lineTo(x(p.t),y(p.value))}else ctx.lineTo(x(p.t),y(p.value));prev=p}ctx.stroke();
  if(windowSec<.5){ctx.fillStyle=s.color;for(const p of path){ctx.beginPath();ctx.arc(x(p.t),y(p.value),2,0,Math.PI*2);ctx.fill()}}
 }
 for(const [t,color,label] of [[hoverTime,'#dae8fa',''],[markerA,'#bc9bff','A'],[markerB,'#f4bb68','B']]){if(t!==null&&t>=start&&t<=end){ctx.setLineDash([4,4]);ctx.strokeStyle=color;ctx.beginPath();ctx.moveTo(x(t),top);ctx.lineTo(x(t),bottom);ctx.stroke();ctx.setLineDash([]);ctx.fillStyle=color;ctx.fillText(label,x(t)+7,top+11)}}
 ctx.restore();
 if(!total){ctx.fillStyle='#8294ac';ctx.textAlign='center';ctx.fillText('此窗口没有有效样本',(left+right)/2,(top+bottom)/2)}
 const t=hoverTime??end;
 chart.box.querySelector('.chart-value').textContent=data.map(({s,all})=>{const p=window.ServalSignals.prior(all,t,hoverTime===null?.25:.05);return s.label+' '+(p?fmt(scaleValue(p.value,kind),4)+' '+unitText(kind)+'  ['+((t-p.t)*1000).toFixed(2)+'ms前]':'—')}).join('     ');
 chart.box.querySelector('.chart-bottom span').textContent=unitText(kind)+' · '+total.toLocaleString()+' 样本';
}
function drawAll(){
 if($('view-scope').hidden)return;
 const end=rangeEnd(),source=freeze?freeze.frames:frames,cache=makeData(source);
 for(const chart of charts)drawChart(chart,cache,end);
 $('view-range').textContent='窗口 '+(end-windowSec).toFixed(4)+' — '+end.toFixed(4)+' s';
 $('cursor-readout').textContent=hoverTime===null?'滚轮缩放 · 拖动平移 · 双击恢复 · 同步游标':'游标 t = '+hoverTime.toFixed(6)+' s（各信号取此前样本，附样本年龄）';
 $('marker-delta').textContent=markerA!==null&&markerB!==null?'Δt '+(Math.abs(markerB-markerA)*1000).toFixed(3)+' ms':'Δt —';
 if(source.length){$('timeline').min=source[0].t;$('timeline').max=Math.max(source[0].t+.001,source[source.length-1].t);$('timeline').value=end;$('cache-range').textContent='缓存 '+source[0].t.toFixed(1)+'–'+source[source.length-1].t.toFixed(1)+'s'}
}
async function requestAnalysis(){
 if($('view-scope').hidden)return;
 const version=++analysisVersion,end=rangeEnd(),start=Math.max(0,end-windowSec);if(end<=start)return;
 try{const response=await fetch('/api/analysis?start='+start+'&end='+end);if(!response.ok)return;const data=await response.json();if(version!==analysisVersion)return;
  $('analysis-count').textContent=data.samples.toLocaleString()+' 个有效对齐 RX 样本';
  for(const axis of ['yaw','pitch']){const d=data[axis];$('metric-'+axis).textContent=fmt(scaleValue(d.rmse??NaN,'angle'),4)+' '+unitText('angle');$('metric-'+axis+'-detail').textContent='P95 '+fmt(scaleValue(d.p95_abs??NaN,'angle'),4)+' / 峰值 '+fmt(scaleValue(d.max_abs??NaN,'angle'),4)+' / 偏差 '+fmt(scaleValue(d.bias??NaN,'angle'),4)}
  $('metric-interval').textContent=fmt(data.sampling.tx.median_ms,3)+' / '+fmt(data.sampling.rx.median_ms,3)+' ms';
  $('metric-gap').textContent='最大间断 '+fmt(data.sampling.tx.max_ms,2)+' / '+fmt(data.sampling.rx.max_ms,2)+' ms';
  $('analysis-method').textContent=data.method+'；统计原始样本，无插值平滑。'+(data.oldest_available!==null&&data.oldest_available>start+.1?'所选窗口前部超出缓存，结果仅涵盖现有数据。':'');
  for(const axis of ['yaw','pitch']){const step=data.step[axis];$('step-'+axis).textContent=axis.toUpperCase()+'：'+(step.status==='ok'?'超调 '+fmt(step.overshoot_percent,2)+'% · 上升 '+fmt(step.rise_ms,2)+'ms · 稳定 '+(step.settling_ms===null?'尚未稳定':fmt(step.settling_ms,2)+'ms'):step.reason)}
 }catch(error){$('analysis-count').textContent='统计暂不可用'}
}
function updateCards(data){
  latest=data.latest;
  const rx=latest.rx,tx=latest.tx;
  const last=Math.max(rx?.t??-Infinity,tx?.t??-Infinity);
  const age=data.now-last;
  $('status').textContent=data.demo?'演示运行中':data.mode==='replay'?(data.replay.playing?'离线回放中':data.replay.position>=data.replay.duration?'回放完成':'回放已暂停'):data.mode==='follow'?(data.follow?.error?'日志读取失败':age<2?'只读监听中':'等待日志更新'):data.bridge.error?'串口已断开':data.mode==='idle'?'未连接串口':age<2?'数据接收中':last===-Infinity?'等待通信数据':'数据已中断';
  $('status-dot').className=age<2?'live':'bad';
  $('source').textContent=data.source;$('demo-badge').hidden=data.source!=='演示数据';
  for(const dir of ['rx','tx'])$(dir+'-rate').innerHTML=`${data.rate[dir].toFixed(0)}<small> Hz</small>`;
  for(const key of ['yaw','pitch']){
    $(key+'-value').textContent=fmt(scaleValue(rx?.values[key],'angle'));
    $(key+'-target').textContent=fmt(scaleValue(tx?.values[key],'angle'));
  }
  $('unit-label').textContent=unitText('angle');
  $('rx-state').textContent=!rx?'未收到':data.now-rx.t<2?'在线':`${(data.now-rx.t).toFixed(1)} 秒前`;
  const txModes=['不控制','控制 / 不开火','控制 / 开火'],rxModes=['空闲','自瞄','小符','大符'];
  $('tx-mode').textContent=txModes[tx?.values.mode]??'—';$('rx-mode').textContent=rxModes[rx?.values.mode]??'—';
  $('q-norm').textContent=fmt(rx?.values.q_norm,4);
  $('rejected').textContent=data.rejected;$('retained').textContent=data.retained.toLocaleString();
  $('error').hidden=!data.last_error;$('error').textContent=data.last_error?`最近拒收：${data.last_error}`:'';
  const bridge=data.bridge;
  $('virtual-field').hidden=!bridge.windows;
  $('link-field').hidden=bridge.windows;
  $('connect').disabled=busy||bridge.running||data.demo||data.mode==='replay'||data.mode==='follow';
  $('disconnect').disabled=busy||!bridge.running||data.demo;
  for(const id of ['physical-port','baud','virtual-port','link-path','record-enabled'])$(id).disabled=bridge.running||data.demo||data.mode==='replay'||data.mode==='follow';
  if(bridge.running&&!$('physical-port').value)$('physical-port').value=bridge.physical;
  $('app-port-line').hidden=!bridge.running;
  $('app-port').textContent=bridge.app_port;
  $('bridge-hint').textContent=data.demo?'当前为模拟数据；实际协议为 TX 30 / RX 44 字节。连接设备或回放日志时请重新启动，不带 --demo。':bridge.running?'桥接正在运行。上位机使用上方虚拟串口；停止桥接会中断通信，设备断开后需手动重连。':bridge.windows?'Windows：先准备虚拟串口对。此处填写工具端 COM，上位机打开另一端。':'Linux：使用固定路径 /tmp/serval-gimbal，首次把上位机 com_port 改为该路径；多实例可指定不同路径。';
  if(bridge.error){$('connection-error').hidden=false;$('connection-error').textContent=bridge.error}
  $('target-id').textContent=tx?.values.target_id??'—';
  const rawColor=rx?.values.enemy_color;
  const mapping=$('color-mapping').value;
  const interpreted=(mapping==='blue-red'?{0:'蓝',1:'红'}:mapping==='red-blue'?{0:'红',1:'蓝'}:{})[rawColor];
  $('enemy-color').textContent=rawColor===undefined?'—':String(rawColor)+(interpreted?' / '+interpreted:' / 未解释');
  $('config-color').textContent=({blue:'蓝',red:'红'})[data.config?.enemy_color]??'未载入（启动加 --config）';
  $('config-color').title=data.config?.path??'';
  const configured=data.config?.enemy_color;
  const mismatch=interpreted&&configured&&interpreted!==({blue:'蓝',red:'红'})[configured];
  const colorSource=data.config?.enemy_color_source;
  $('color-note').textContent=colorSource==='config'?'YAML 设置为固定颜色；该设置需新版自瞄支持并重新启动才生效。RX 原值仍是电控上报，页面不改写它。':mismatch?'RX 颜色解释与 YAML 初值不一致；按原版串口颜色逻辑会覆盖初值。请核对正在运行的程序版本。':'这里只读取原始字节；颜色解释选项只影响文字显示，不改变检测、跟踪或串口。';
  const following=data.mode==='follow';
  $('follow-start').disabled=busy||bridge.running||data.demo||data.mode==='replay'||following;
  $('follow-stop').disabled=busy||!following;
  $('follow-path').disabled=following||busy;
  $('follow-status').textContent=data.follow?(data.follow.error||((data.follow.current??'等待目录中出现 .bin')+' · '+data.follow.records+' 条记录')):'默认从 records/serial 读取，无需虚拟串口';
  $('mode-note').textContent=following?'只读监听：不打开任何串口，停止监听不影响视觉控制':'桥接模式参与数据传输；只读模式只读取日志';
  $('crc-count').textContent=data.errors?.crc??0;
  $('gap-max').textContent=fmt(data.max_gap_ms.tx,1)+' / '+fmt(data.max_gap_ms.rx,1)+' ms';
  $('forward-max').textContent=data.mode==='live'?fmt(bridge.max_forward_ms?.tx,2)+' / '+fmt(bridge.max_forward_ms?.rx,2)+' ms':'—';
  $('tracking-error').textContent=data.tracking?fmt(scaleValue(data.tracking.yaw,'angle'))+' / '+fmt(scaleValue(data.tracking.pitch,'angle'))+' '+unitText('angle'):'—';
  $('tracking-error').title=data.tracking?'两次采样间隔 '+fmt(data.tracking.sample_offset_ms,2)+' ms；不是同步设备误差':'仅比较 250 ms 内、相差不超过 50 ms 且处于控制模式的采样';
  const recording=data.recording;
  $('log-path-line').hidden=!recording;
  $('log-path').textContent=recording?(recording.active?'录制中 · ':'已保存 · ')+recording.path+' ('+recording.written+' 帧)':'';
  if(recording?.error){$('connection-error').hidden=false;$('connection-error').textContent='日志写入失败：'+recording.error}
  const replay=data.replay;
  $('load-replay').disabled=busy||bridge.running||data.demo;
  $('replay-file').disabled=busy||bridge.running||data.demo;
  $('replay-play').disabled=busy||!replay;
  $('replay-restart').disabled=busy||!replay;
  $('replay-stop').disabled=busy||!replay;
  $('replay-speed').disabled=busy||!replay;
  $('replay-play').textContent=replay?.playing?'暂停回放':'继续回放';
  $('replay-time').textContent=replay?fmt(replay.position,1)+' / '+fmt(replay.duration,1)+' s · '+replay.records+' 条记录':'未加载';
  if(replay){$('replay-speed').value=String(replay.speed);if(replay.truncated_bytes)$('replay-time').textContent+=' · 已跳过末尾截断记录'}
  if(data.dropped)sampleDrops+=data.dropped;
  $('notice').hidden=!sampleDrops&&!bridge.preview_drops&&!recording?.dropped;
  $('notice').textContent=`浏览器跳过 ${sampleDrops.toLocaleString()} 帧；预览队列丢弃 ${bridge.preview_drops} 块；日志队列丢弃 ${recording?.dropped??0} 帧。串口转发独立运行，CSV 保留最近 ${data.capacity.toLocaleString()} 帧。`;
}

let lastData=null;
async function poll(){
  try{
    const response=await fetch(`/api/data?since=${cursor}`,{signal:AbortSignal.timeout(4000)});
    if(!response.ok)throw new Error(`HTTP ${response.status}`);
    const data=await response.json();
    if(epoch!==data.epoch){frames=[];freeze=null;viewEnd=null;markerA=markerB=hoverTime=null;cutoff=0;sampleDrops=0;epoch=data.epoch;$('pause').textContent='Ⅱ 暂停曲线';$('pause').classList.remove('active')}
    cursor=data.cursor;serverNow=data.now;lastPoll=performance.now();lastData=data;
    frames.push(...data.frames);frames.sort((a,b)=>a.t-b.t||a.seq-b.seq);if(frames.length>60000)frames.splice(0,frames.length-60000);
    while(frames.length&&frames[0].t<serverNow-120)frames.shift();
    updateCards(data);drawAll();
  }catch(error){$('status').textContent='监听服务未连接';$('status-dot').className='bad';$('source').textContent='请确认 monitor.py 正在运行'}
  setTimeout(poll,100);
}
$('pause').addEventListener('click',()=>{
  if(freeze){freeze=null;viewEnd=null;$('pause').textContent='Ⅱ 暂停曲线';$('pause').classList.remove('active')}else freezeNow();drawAll();requestAnalysis();
});
$('clear').addEventListener('click',()=>{cutoff=serverNow;frames=[];if(freeze)freeze={frames:[],now:serverNow};drawAll()});
$('window').addEventListener('change',ev=>{windowSec=+ev.target.value;drawAll();requestAnalysis()});
$('unit').addEventListener('change',ev=>{unit=ev.target.value;charts.forEach(c=>c.limits=null);if(lastData)updateCards({...lastData,dropped:0});drawAll();requestAnalysis()});
$('fixed-y').addEventListener('click',()=>{fixedY=!fixedY;$('fixed-y').textContent=fixedY?'Y轴已锁定':'Y轴自动';$('fixed-y').classList.toggle('active',fixedY);drawAll()});
$('angle-mode').addEventListener('change',drawAll);
$('reset-view').addEventListener('click',()=>{viewEnd=null;freeze=null;hoverTime=null;windowSec=+$('window').value;$('pause').textContent='Ⅱ 暂停曲线';$('pause').classList.remove('active');drawAll();requestAnalysis()});
$('timeline').addEventListener('input',ev=>{freezeNow();viewEnd=+ev.target.value;drawAll();requestAnalysis()});
for(const name of ['a','b'])$('marker-'+name).addEventListener('click',()=>{const t=lastProbeTime??rangeEnd();if(name==='a')markerA=t;else markerB=t;drawAll()});
$('marker-clear').addEventListener('click',()=>{markerA=markerB=null;drawAll()});
document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>{
  document.querySelectorAll('[data-view]').forEach(b=>{b.classList.toggle('selected',b===button);b.setAttribute('aria-selected',String(b===button))});
  document.querySelectorAll('.view-panel').forEach(panel=>panel.hidden=panel.id!=='view-'+button.dataset.view);
  drawAll();requestAnalysis();
}));
setInterval(()=>{if(!freeze)requestAnalysis()},1500);
$('imu').addEventListener('toggle',drawAll);window.addEventListener('resize',drawAll);
document.querySelectorAll('[data-channel]').forEach(button=>button.addEventListener('click',()=>{
  channel=button.dataset.channel;document.querySelectorAll('[data-channel]').forEach(b=>b.classList.toggle('selected',b===button));
  charts.filter(c=>c.group).forEach(c=>c.box.hidden=channel!=='all'&&c.group!==channel);
  $('channel-note').textContent=channel==='accel'?'加速度只有 TX 实际下发值；协议不含 RX 加速度，不以微分估计冒充回传值':channel==='error'?'辅助计算量：此前实际 TX 减去当前 RX，Yaw 取最短角差；只统计控制段':'含 mode=0 原始下发值；不控制段与控制段断开显示';
  drawAll();
}));
async function refreshPorts(){
  try{const ports=await(await fetch('/api/ports')).json();$('ports').replaceChildren();
    for(const port of ports){const option=document.createElement('option');option.value=port.device;option.label=port.description;$('ports').append(option)}
  }catch(error){$('connection-error').hidden=false;$('connection-error').textContent='无法读取串口列表，请确认监听服务已启动'}
}
async function control(path,args={}){
  busy=true;$('connect').disabled=true;$('disconnect').disabled=true;$('connection-error').hidden=true;
  try{const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(args)});const result=await response.json();if(!response.ok)throw new Error(result.error||'串口操作失败')}
  catch(error){$('connection-error').hidden=false;$('connection-error').textContent=error.message}
  finally{busy=false}
}
$('connect-form').addEventListener('submit',ev=>{ev.preventDefault();control('/api/start',{port:$('physical-port').value.trim(),baud:+$('baud').value,virtual:$('virtual-port').value.trim(),link:$('link-path').value.trim(),record:$('record-enabled').checked})});
$('disconnect').addEventListener('click',()=>control('/api/stop'));
$('refresh-ports').addEventListener('click',refreshPorts);
$('follow-form').addEventListener('submit',ev=>{ev.preventDefault();control('/api/follow',{path:$('follow-path').value.trim()})});
$('follow-stop').addEventListener('click',()=>control('/api/stop'));
$('color-mapping').addEventListener('change',()=>{if(lastData)updateCards({...lastData,dropped:0})});
$('replay-play').addEventListener('click',()=>control('/api/replay/control',{action:lastData?.replay?.playing?'pause':'play'}));
$('replay-restart').addEventListener('click',()=>control('/api/replay/control',{action:'restart'}));
$('replay-speed').addEventListener('change',ev=>control('/api/replay/control',{action:'speed',speed:+ev.target.value}));
$('replay-stop').addEventListener('click',()=>control('/api/stop'));
$('load-replay').addEventListener('click',async()=>{
  const file=$('replay-file').files[0];
  if(!file){$('connection-error').hidden=false;$('connection-error').textContent='请先选择原工程 .bin 日志';return}
  busy=true;$('connection-error').hidden=true;
  try{
    const response=await fetch('/api/replay/file',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-File-Name':encodeURIComponent(file.name)},body:file});
    const result=await response.json();if(!response.ok)throw new Error(result.error||'日志加载失败');
  }catch(error){$('connection-error').hidden=false;$('connection-error').textContent=error.message}
  finally{busy=false}
});
refreshPorts();
poll();
