'use strict';
const $ = id => document.getElementById(id);
const colors={tx:'#61b7ff',rx:'#38d9b6',imu:'#bc9bff',second:'#f4bb68',third:'#f27a91',fourth:'#38d9b6'};
/* QtWebEngine 5.15 (Chromium 83) 没有 AbortSignal.timeout；统一用兼容 helper。 */
const timeoutSignal=ms=>{
  if(typeof AbortSignal!=='undefined'&&typeof AbortSignal.timeout==='function')return AbortSignal.timeout(ms);
  const controller=new AbortController();setTimeout(()=>controller.abort(),ms);return controller.signal;
};
const scheduleFrame=(typeof requestAnimationFrame==='function')?requestAnimationFrame.bind(window):callback=>setTimeout(callback,16);
const clearNode=node=>{while(node.firstChild)node.removeChild(node.firstChild)};
let frames=[],cursor=0,serverNow=0,lastPoll=performance.now(),unit='deg',windowSec=10;
let freeze=null,cutoff=0,latest={},charts=[],sampleDrops=0,epoch=null,busy=false;
let viewEnd=null,hoverTime=null,lastProbeTime=null,markerA=null,markerB=null,drag=null,analysisVersion=0,channel='angle',fixedY=false;
let lastData=null,lastAppliedSeq=0,drawQueued=false,pendingCards=null,pauseResetPending=false;
let stream=null,streamConnected=false,streamRetry=null,pollTimer=null;
let lastCardsAt=0,lastDetailAt=0,lastReadoutAt=0,lastChartReadoutAt=0,frameCap=60000;
/* 细节模式 / 双通信时序 / 刷新率统计（第二轮）。 */
let detailMode=false,duplexWindow=0.5,lastDetailGaps=[];
let drawFps=0,drawCount=0,drawWindowStart=performance.now();
let pushHz=0,pushEvents=0,pushCount=0,pushWindowStart=performance.now(),lastPushAt=null;
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
/* live_udp 的帧 t 在发送端纳秒时间轴上；用“最新帧 t + 服务端收包年龄”得到同域 now，
   即使后端 data.now 仍按本机 Store 起点计时，窗口也不会错位。 */
function snapshotNow(data){
  if(data&&data.mode==='live_udp'&&data.udp){
    const latest=Math.max(data.latest?.rx?.t??-Infinity,data.latest?.tx?.t??-Infinity);
    if(Number.isFinite(latest)&&Number.isFinite(data.udp.age))return latest+data.udp.age;
  }
  return data?data.now:serverNow;
}
function rangeEnd(){return viewEnd??(freeze?freeze.now:snapshotNow(lastData)+(lastData?.mode==='replay'?0:(performance.now()-lastPoll)/1000))}
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
 const gapTimes=[];
 for(const {s,points} of data){
   const detail=detailMode;
   // 细节模式：不调用 envelope()，逐样本连线；否则按像素列包络降采样。
   const path=detail?points:window.ServalSignals.envelope(points,right-left,x);
   ctx.strokeStyle=s.color;ctx.lineWidth=detail?1.2:1.7;ctx.beginPath();let prev=null;
   for(const p of path){if(!prev||p.segment!==prev.segment||p.t-prev.t>.1)ctx.moveTo(x(p.t),y(p.value));else if(s.direction==='tx'){ctx.lineTo(x(p.t),y(prev.value));ctx.lineTo(x(p.t),y(p.value))}else ctx.lineTo(x(p.t),y(p.value));prev=p}ctx.stroke();
   // 小窗口或稀疏时始终画 2px 样本点，便于看出单样异常。
   if(windowSec<=2||points.length<=1500){ctx.fillStyle=s.color;for(const p of points){ctx.beginPath();ctx.arc(x(p.t),y(p.value),2,0,Math.PI*2);ctx.fill()}}
   // segment 变化或相邻 Δt 超过阈值：2px 红色异常间断竖线（同帧去重）。
   for(const mark of window.ServalSignals.gapMarks(points,gapThresholdMs())){if(mark.t>=start&&mark.t<=end&&gapTimes.indexOf(mark.t)<0)gapTimes.push(mark.t)}
 }
 ctx.strokeStyle='#ff6b7a';ctx.lineWidth=2;
 for(const t of gapTimes){ctx.beginPath();ctx.moveTo(x(t),top);ctx.lineTo(x(t),bottom);ctx.stroke()}
 for(const [t,color,label] of [[hoverTime,'#dae8fa',''],[markerA,'#bc9bff','A'],[markerB,'#f4bb68','B']]){if(t!==null&&t>=start&&t<=end){ctx.setLineDash([4,4]);ctx.strokeStyle=color;ctx.beginPath();ctx.moveTo(x(t),top);ctx.lineTo(x(t),bottom);ctx.stroke();ctx.setLineDash([]);ctx.fillStyle=color;ctx.fillText(label,x(t)+7,top+11)}}
 ctx.restore();
 if(!total){ctx.fillStyle='#8294ac';ctx.textAlign='center';ctx.fillText('此窗口没有有效样本',(left+right)/2,(top+bottom)/2)}
 // 读数文本 10 Hz 足够；鼠标游标时随指针实时更新，避免每帧 8 次 DOM 写入。
 const readNow=performance.now();
 if(hoverTime!==null||readNow-lastChartReadoutAt>=100){
  lastChartReadoutAt=readNow;
  const t=hoverTime??end;
  chart.box.querySelector('.chart-value').textContent=data.map(({s,all})=>{const p=window.ServalSignals.prior(all,t,hoverTime===null?.25:.05);return s.label+' '+(p?fmt(scaleValue(p.value,kind),4)+' '+unitText(kind)+'  ['+((t-p.t)*1000).toFixed(2)+'ms前]':'—')}).join('     ');
  let caption=unitText(kind)+' · '+total.toLocaleString()+' 样本';
  if(detailMode)caption+=' · 逐点细节';
  if(gapTimes.length)caption+=' · 红色=异常间断 '+gapTimes.length+' 处';
  chart.box.querySelector('.chart-bottom span').textContent=caption;
 }
}
function gapThresholdMs(){
 // 细节模式用后端 detail 的间隔中位数自适应阈值（≥5ms）；普通模式维持 100ms。
 const udp=lastData&&lastData.udp,detail=udp&&udp.detail;
 if(detailMode&&detail&&detail.tx&&Number.isFinite(detail.tx.interval_median_ms)){
  const tx=detail.tx.interval_median_ms;
  const rx=detail.rx&&Number.isFinite(detail.rx.interval_median_ms)?detail.rx.interval_median_ms:tx;
  const base=Math.max(tx,rx);
  if(base>0)return Math.max(5,Math.min(100,3*base));
 }
 return 100;
}
function chartVisible(chart){
 // 只绘制视口附近的图表：8 张波形在滚动容器里通常只有 2~3 张可见，
 // 跳过大片离屏 canvas 是高采样率下最有效的降负载手段。
 const box=chart.box;if(!box||box.hidden)return false;
 const rect=box.getBoundingClientRect();
 if(!rect.width||!rect.height)return false;
 const viewport=window.innerHeight||document.documentElement.clientHeight||0;
 return rect.bottom>=-100&&rect.top<=viewport+100;
}
function drawAll(){
 if($('view-scope').hidden)return;
 const end=rangeEnd(),source=freeze?freeze.frames:frames;
 // 只截取窗口附近的一小段（发送端时间有序），避免每帧对全部历史帧做 filter/sort。
 let view=source;
 if(source.length){
  const low=Math.max(cutoff,end-windowSec-2);
  let lo=0,hi=source.length;
  while(lo<hi){const mid=(lo+hi)>>1;if(source[mid].t<low)lo=mid+1;else hi=mid}
  const startIndex=Math.max(0,lo-2);
  let lo2=lo,hi2=source.length;
  while(lo2<hi2){const mid=(lo2+hi2)>>1;if(source[mid].t<=end+.5)lo2=mid+1;else hi2=mid}
  view=source.slice(startIndex,lo2+1);
 }
 const cache=makeData(view);
 for(const chart of charts){if(chartVisible(chart))drawChart(chart,cache,end)}
 const now=performance.now();
 if(now-lastReadoutAt>=100){
  lastReadoutAt=now;
  $('view-range').textContent='窗口 '+(end-windowSec).toFixed(4)+' — '+end.toFixed(4)+' s';
  $('cursor-readout').textContent=hoverTime===null?'滚轮缩放 · 拖动平移 · 双击恢复 · 同步游标':'游标 t = '+hoverTime.toFixed(6)+' s（各信号取此前样本，附样本年龄）';
  $('marker-delta').textContent=markerA!==null&&markerB!==null?'Δt '+(Math.abs(markerB-markerA)*1000).toFixed(3)+' ms':'Δt —';
  if(source.length){$('timeline').min=source[0].t;$('timeline').max=Math.max(source[0].t+.001,source[source.length-1].t);$('timeline').value=end;$('cache-range').textContent='缓存 '+source[0].t.toFixed(1)+'–'+source[source.length-1].t.toFixed(1)+'s'}
 }
}
function countDrawFrame(){
 const now=performance.now();
 drawCount++;
 if(now-drawWindowStart>=1000){drawFps=drawCount*1000/(now-drawWindowStart);drawCount=0;drawWindowStart=now;}
 if(now-pushWindowStart>=1000){pushHz=pushCount*1000/(now-pushWindowStart);pushCount=0;pushWindowStart=now;}
}
function requestDraw(){
  // 数据可能数百 Hz 到达；每帧最多一次 DOM 更新 + 绘制。页面隐藏时只收数据不绘制。
  if(drawQueued||document.hidden)return;
  drawQueued=true;
  scheduleFrame(flushFrame);
}
function flushFrame(){
  drawQueued=false;
  if(document.hidden)return;
  if(pauseResetPending){pauseResetPending=false;$('pause').textContent='Ⅱ 暂停曲线';$('pause').classList.remove('active')}
  const data=pendingCards;pendingCards=null;
  const now=performance.now();
  // 文本/DOM 10 Hz 足够读数，避免高推送率下每帧几十次 DOM 写入与重排。
  if(data&&(now-lastCardsAt>=100||lastCardsAt===0)){
    lastCardsAt=now;
    updateCards(data);
    try{if(window.ServalNative&&window.ServalNative.onSnapshot)window.ServalNative.onSnapshot(data)}catch(error){}
    if(now-lastDetailAt>=100){lastDetailAt=now;updateDetailPanels(data)}
  }
  if(!$('view-scope').hidden){
    drawAll();
    drawDuplex();
    countDrawFrame();
    refreshDebugSnapshot();
  }
}
async function requestAnalysis(){ if($('view-scope').hidden)return;
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
  const liveUdp=data.mode==='live_udp';
  const udp=liveUdp?window.ServalSignals.udpCounters(data.udp):null;
  const udpAge=liveUdp?(udp.age!==null?udp.age:(last===-Infinity?null:Math.max(0,age))):null;
  const now=liveUdp?snapshotNow(data):data.now;
  $('status').textContent=liveUdp?(udpAge===null?'等待 UDP 数据':udpAge<2?'UDP 实时接收中':'UDP 已中断'):data.demo?'演示运行中':data.mode==='replay'?(data.replay.playing?'离线回放中':data.replay.position>=data.replay.duration?'回放完成':'回放已暂停'):data.mode==='follow'?(data.follow?.error?'日志读取失败':age<2?'只读监听中':'等待日志更新'):data.bridge.error?'串口已断开':data.mode==='idle'?'未连接串口':age<2?'数据接收中':last===-Infinity?'等待通信数据':'数据已中断';
  $('status-dot').className=liveUdp?(udpAge!==null&&udpAge<2?'live':'bad'):(age<2?'live':'bad');
  $('source').textContent=data.source;$('demo-badge').hidden=data.source!=='演示数据';
  for(const dir of ['rx','tx'])$(dir+'-rate').innerHTML=`${data.rate[dir].toFixed(0)}<small> Hz</small>`;
  for(const key of ['yaw','pitch']){
    $(key+'-value').textContent=fmt(scaleValue(rx?.values[key],'angle'));
    $(key+'-target').textContent=fmt(scaleValue(tx?.values[key],'angle'));
  }
  $('unit-label').textContent=unitText('angle');
  $('rx-state').textContent=!rx?'未收到':now-rx.t<2?'在线':`${(now-rx.t).toFixed(1)} 秒前`;
  const txModes=['不控制','控制 / 不开火','控制 / 开火'],rxModes=['空闲','自瞄','小符','大符'];
  $('tx-mode').textContent=txModes[tx?.values.mode]??'—';$('rx-mode').textContent=rxModes[rx?.values.mode]??'—';
  $('q-norm').textContent=fmt(rx?.values.q_norm,4);
  $('rejected').textContent=data.rejected;$('retained').textContent=data.retained.toLocaleString();
  $('error').hidden=!data.last_error;$('error').textContent=data.last_error?`最近拒收：${data.last_error}`:'';
  // UDP 实时接收时显示缺序号/迟到/非法包与来源；其他模式整块隐藏。
  $('udp-block').hidden=!liveUdp;
  if(liveUdp){
    $('udp-peer').textContent=udp.peer;
    $('udp-missing').textContent=udp.missing.toLocaleString();
    $('udp-late').textContent=udp.late.toLocaleString();
    $('udp-invalid').textContent=udp.invalid.toLocaleString();
    $('udp-fps').textContent=udp.fps===null?'—':fmt(udp.fps,2)+' Hz';
    $('udp-session').textContent=udp.session===null?'等待会话':'会话 '+udp.session;
    $('udp-error').hidden=!udp.error;$('udp-error').textContent=udp.error?('UDP 错误：'+udp.error):'';
  }else{
    $('udp-error').hidden=true;$('udp-error').textContent='';
  }
  const bridge=data.bridge;
  $('virtual-field').hidden=!bridge.windows;
  $('link-field').hidden=bridge.windows;
  $('connect').disabled=busy||bridge.running||data.demo||data.mode==='replay'||data.mode==='follow'||liveUdp;
  $('disconnect').disabled=busy||!bridge.running||data.demo;
  for(const id of ['physical-port','baud','virtual-port','link-path','record-enabled','refresh-ports'])$(id).disabled=bridge.running||data.demo||data.mode==='replay'||data.mode==='follow'||liveUdp;
  if(bridge.running&&!$('physical-port').value)$('physical-port').value=bridge.physical;
  $('app-port-line').hidden=!bridge.running;
  $('app-port').textContent=bridge.app_port;
  $('bridge-hint').textContent=liveUdp?'UDP 实时接收模式不打开串口，桥接表单仅在串口模式下可用；切换数据源请停止 monitor.py 后重新启动。':data.demo?'当前为模拟数据；实际协议为 TX 30 / RX 44 字节。连接设备或回放日志时请重新启动，不带 --demo。':bridge.running?'桥接正在运行。上位机使用上方虚拟串口；停止桥接会中断通信，设备断开后需手动重连。':bridge.windows?'Windows：先准备虚拟串口对。此处填写工具端 COM，上位机打开另一端。':'Linux：使用固定路径 /tmp/serval-gimbal，首次把上位机 com_port 改为该路径；多实例可指定不同路径。';
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
  $('session-live-note').hidden=!liveUdp;
  $('follow-start').disabled=busy||bridge.running||data.demo||data.mode==='replay'||following||liveUdp;
  $('follow-stop').disabled=busy||!following;
  $('follow-path').disabled=following||busy||liveUdp;
  $('follow-status').textContent=liveUdp?'UDP 实时接收模式不支持只读日志监听；请重启 monitor.py 并选择 --follow。':data.follow?(data.follow.error||((data.follow.current??'等待目录中出现 .bin')+' · '+data.follow.records+' 条记录')):'默认从 records/serial 读取，无需虚拟串口';
  $('mode-note').textContent=liveUdp?'UDP 实时接收：不打开串口；桥接/监听/回放已禁用，切换数据源需重启 monitor.py':following?'只读监听：不打开任何串口，停止监听不影响视觉控制':'桥接模式参与数据传输；只读模式只读取日志';
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
  $('load-replay').disabled=busy||bridge.running||data.demo||liveUdp;
  $('replay-file').disabled=busy||bridge.running||data.demo||liveUdp;
  $('replay-play').disabled=busy||!replay;
  $('replay-restart').disabled=busy||!replay;
  $('replay-stop').disabled=busy||!replay;
  $('replay-speed').disabled=busy||!replay;
  $('replay-play').textContent=replay?.playing?'暂停回放':'继续回放';
  $('replay-time').textContent=replay?fmt(replay.position,1)+' / '+fmt(replay.duration,1)+' s · '+replay.records+' 条记录':'未加载';
  if(replay){$('replay-speed').value=String(replay.speed);if(replay.truncated_bytes)$('replay-time').textContent+=' · 已跳过末尾截断记录'}
  $('notice').hidden=!sampleDrops&&!bridge.preview_drops&&!recording?.dropped;
  $('notice').textContent=liveUdp?`页面更新合并跳过 ${sampleDrops.toLocaleString()} 帧；UDP 原始包缓存保留最近 ${data.capacity.toLocaleString()} 帧，原始 .bin 记录逐字节保留。`:`浏览器跳过 ${sampleDrops.toLocaleString()} 帧；预览队列丢弃 ${bridge.preview_drops} 块；日志队列丢弃 ${recording?.dropped??0} 帧。串口转发独立运行，CSV 保留最近 ${data.capacity.toLocaleString()} 帧。`;
  updateCamera(data.camera);
}

/* 相机预览：仅在后端 data.camera.available 时显示 MJPEG 流，避免无相机时反复请求 404。
   高度受控并用“收起/展开”按钮让调试版用户能把相机与两张波形同屏（仅本次会话记忆）。 */
let cameraRequested=false,cameraCollapsed=false;
function syncCameraCollapse(){
  const collapsed=!!cameraCollapsed;
  $('camera-body').hidden=collapsed;
  $('camera-panel').classList.toggle('collapsed',collapsed);
  const toggle=$('camera-toggle');
  toggle.textContent=collapsed?'展开':'收起';
  toggle.setAttribute('aria-expanded',String(!collapsed));
  requestDraw();
}
function updateCamera(camera){
  const panel=$('camera-panel'),available=!!(camera&&camera.available);
  if(!available){
    if(!panel.hidden){
      panel.hidden=true;cameraRequested=false;$('camera-feed').removeAttribute('src');
      $('camera-placeholder').hidden=false;$('camera-placeholder').textContent='等待相机画面';
    }
    return;
  }
  panel.hidden=false;
  $('camera-body').hidden=cameraCollapsed;
  $('camera-fps').textContent=camera.fps?'目标 '+camera.fps+' FPS':'—';
  if(!cameraRequested){cameraRequested=true;$('camera-feed').src='/api/camera.mjpg'}
}
$('camera-toggle').addEventListener('click',()=>{cameraCollapsed=!cameraCollapsed;syncCameraCollapse()});
function updateTransport(streaming){
  $('transport-mode').textContent=streaming?'SSE 推送':'HTTP 轮询（回退）';
  $('transport-detail').textContent=streaming?'有变化即推、服务端合并；页面 rAF 合并绘制，不是 0 延迟':'浏览器或旧服务回退模式，约 100 ms 轮询一次';
}
/* ---------- 双通信时序 / 细节统计（第二轮） ---------- */
const setText=(id,value)=>{const element=$(id);if(element&&element.textContent!==value)element.textContent=value};
function recentFrames(seconds){
  // frames 按 t 升序；从尾部倒查最近 N 秒，避免每帧全量扫描。
  const end=snapshotNow(lastData);
  const list=[];
  if(!Number.isFinite(end))return {end:NaN,start:NaN,list};
  const start=end-seconds;
  for(let index=frames.length-1;index>=0;index--){
    const frame=frames[index];
    if(frame.t>end)continue;
    if(frame.t<start)break;
    list.push(frame);
  }
  list.reverse();
  return {end,start,list};
}
function detailSideText(side,detail){
  const stats=detail&&detail[side];
  if(!stats)return {main:'—',small:detail?'等待该方向 detail 样本':'等待后端 detail（data.udp.detail）'};
  if(!Number.isFinite(stats.interval_median_ms))return {main:'—',small:'该方向样本不足，尚无间隔统计'};
  const main=fmt(stats.interval_median_ms,2)+' / '+fmt(stats.interval_p95_ms,2)+' / '+fmt(stats.interval_max_ms,2)+' ms';
  const parts=['jitter '+fmt(stats.interval_jitter_ms,2)+' ms'];
  if(Number.isFinite(stats.arrival_median_ms))parts.push('网络到达 '+fmt(stats.arrival_median_ms,2)+' / '+fmt(stats.arrival_p95_ms,2)+' ms');
  if(Number.isFinite(stats.count))parts.push(stats.count.toLocaleString()+' 样本');
  return {main,small:parts.join(' · ')};
}
function duplexCacheStats(){
  const {end,list}=recentFrames(2);
  if(!Number.isFinite(end))return null;
  const tx=[],rx=[];
  for(const frame of list)(frame.direction==='tx'?tx:rx).push({t:frame.t});
  if(!tx.length||!rx.length)return null;
  const pairs=window.ServalSignals.duplexPairs(tx,rx,100);
  if(!pairs.length)return null;
  const ages=pairs.map(pair=>pair.age).sort((a,b)=>a-b);
  return {count:pairs.length,median:ages[(ages.length-1)>>1],max:ages[ages.length-1]};
}
function fallbackGaps(){
  const {list}=recentFrames(2);
  const result=[];
  for(const direction of ['tx','rx']){
    const points=[];
    for(const frame of list)if(frame.direction===direction)points.push({t:frame.t,segment:0});
    for(const mark of window.ServalSignals.gapMarks(points,100))result.push({t:mark.t,ms:mark.ms,direction});
  }
  result.sort((a,b)=>a.t-b.t);
  return result.slice(-6).reverse();
}
function updateDuplexGapList(detail){
  let gaps=detail&&Array.isArray(detail.gaps)?detail.gaps.filter(gap=>gap&&Number.isFinite(gap.t)&&Number.isFinite(gap.ms)):[];
  if(!gaps.length)gaps=fallbackGaps();
  // 后端约定 gaps 最近优先；这里再按 t 降序兜底，兼容旧后端/缓存推算结果。
  const recent=gaps.slice().sort((a,b)=>b.t-a.t).slice(0,6);
  setText('duplex-gaps',recent.length?recent.map(gap=>`${String(gap.direction||'?').toUpperCase()} t=${Number(gap.t).toFixed(3)}s · ${Number(gap.ms).toFixed(1)}ms`).join('    '):'—');
}
function updateDuplexOffset(){
  const stats=duplexCacheStats();
  if(!stats){setText('duplex-offset','—');setText('duplex-offset-detail','由页面缓存 frames 计算，不使用服务端降采样');return}
  setText('duplex-offset',fmt(stats.median,2)+' / '+fmt(stats.max,2)+' ms');
  setText('duplex-offset-detail',stats.count.toLocaleString()+' 对 RX→TX（≤100ms，最近 2s 缓存）');
}
function drawDuplex(){
  const canvas=$('duplex-canvas');
  if(!canvas||$('view-scope').hidden)return;
  const rect=canvas.getBoundingClientRect();if(!rect.width||!rect.height)return;
  const ratio=window.devicePixelRatio||1,w=rect.width,h=rect.height;
  if(canvas.width!==Math.round(w*ratio)||canvas.height!==Math.round(h*ratio)){canvas.width=Math.round(w*ratio);canvas.height=Math.round(h*ratio)}
  const ctx=canvas.getContext('2d');ctx.setTransform(ratio,0,0,ratio,0,0);ctx.clearRect(0,0,w,h);
  const {end,list}=recentFrames(duplexWindow);
  if(!Number.isFinite(end)){ctx.fillStyle='#8294ac';ctx.font='10px Consolas, monospace';ctx.textAlign='center';ctx.fillText('等待 TX/RX 样本',w/2,h/2);return}
  const start=end-duplexWindow,left=42,right=w-14,top=10,bottom=h-18;
  const tx=[],rx=[];
  for(const frame of list)(frame.direction==='tx'?tx:rx).push({t:frame.t});
  const x=t=>left+(t-start)/duplexWindow*(right-left);
  const txY=top+(bottom-top)*0.30,rxY=top+(bottom-top)*0.70;
  ctx.font='9px Consolas, monospace';ctx.lineWidth=1;
  for(let i=0;i<=4;i++){
    const t=start+duplexWindow*i/4,xx=x(t);
    ctx.strokeStyle='#1d2a3d';ctx.beginPath();ctx.moveTo(xx,top);ctx.lineTo(xx,bottom);ctx.stroke();
    ctx.fillStyle='#7f93af';ctx.textAlign='center';
    ctx.fillText((duplexWindow<1?((t-start)).toFixed(3):(t-start).toFixed(2))+'s',xx,bottom+13);
  }
  ctx.strokeStyle='#2b3d55';ctx.beginPath();ctx.moveTo(left,txY);ctx.lineTo(right,txY);ctx.moveTo(left,rxY);ctx.lineTo(right,rxY);ctx.stroke();
  ctx.textAlign='left';ctx.fillStyle=colors.tx;ctx.fillText('TX',4,txY+3);ctx.fillStyle=colors.rx;ctx.fillText('RX',4,rxY+3);
  ctx.strokeStyle=colors.tx;
  for(const point of tx){const xx=x(point.t);ctx.beginPath();ctx.moveTo(xx,txY-6);ctx.lineTo(xx,txY+6);ctx.stroke()}
  ctx.strokeStyle=colors.rx;
  for(const point of rx){const xx=x(point.t);ctx.beginPath();ctx.moveTo(xx,rxY-6);ctx.lineTo(xx,rxY+6);ctx.stroke()}
  const pairs=window.ServalSignals.duplexPairs(tx,rx,100);
  ctx.strokeStyle='rgba(120,170,220,0.35)';ctx.lineWidth=0.7;
  for(const pair of pairs){ctx.beginPath();ctx.moveTo(x(pair.tx.t),txY+6);ctx.lineTo(x(pair.rx.t),rxY-6);ctx.stroke()}
  // 异常缺口：后端 detail.gaps 优先，缺失时用缓存 frames 推算（Δ>100ms）。
  let marks=lastDetailGaps.filter(gap=>Number.isFinite(gap.t)&&gap.t>=start&&gap.t<=end).map(gap=>gap.t);
  if(!marks.length){
    const threshold=gapThresholdMs();
    for(const series of [tx,rx])for(let index=1;index<series.length;index++)if((series[index].t-series[index-1].t)*1000>threshold)marks.push(series[index].t);
  }
  ctx.strokeStyle='#ff6b7a';ctx.lineWidth=2;
  for(const t of marks){ctx.beginPath();ctx.moveTo(x(t),top);ctx.lineTo(x(t),bottom);ctx.stroke()}
  if(!tx.length&&!rx.length){ctx.fillStyle='#8294ac';ctx.font='10px Consolas, monospace';ctx.textAlign='center';ctx.fillText('此时序窗口没有 TX/RX 样本',(left+right)/2,(top+bottom)/2)}
}
function updateDetailPanels(data){
  const udp=data&&data.udp,detail=udp&&udp.detail;
  lastDetailGaps=detail&&Array.isArray(detail.gaps)?detail.gaps.filter(gap=>gap&&Number.isFinite(gap.t)&&Number.isFinite(gap.ms)).slice(-32):[];
  const tx=detailSideText('tx',detail),rx=detailSideText('rx',detail);
  setText('duplex-tx-interval',tx.main);setText('duplex-tx-detail',tx.small);
  setText('duplex-rx-interval',rx.main);setText('duplex-rx-detail',rx.small);
  updateDuplexOffset();
  updateDuplexGapList(detail);
  const visible=document.hidden?'页面隐藏':'页面可见';
  const target=lastData&&Number.isFinite(lastData.push_hz)?lastData.push_hz:null;
  setText('refresh-rate',fmt(pushHz,1)+' Hz / '+drawFps.toFixed(0)+' FPS');
  setText('refresh-note','推送 '+(streamConnected?fmt(pushHz,1)+(target!==null?'/'+fmt(target,0):'')+' Hz':'回退轮询')+' · 绘制 '+fmt(drawFps,1)+' FPS · '+visible);
}
/* 收到一份 /api/data 形状的快照（轮询或 SSE 均复用）：只做时间轴/缓存状态，
   DOM 更新与绘制全部合并到下一帧（flushFrame），避免高频数据时每个包都改页面。 */
function applyData(data){
  // epoch 变化 = 服务端 store.reset；cursor 回退 = monitor.py 重启后的新序号空间。
  if(epoch!==data.epoch||data.cursor<lastAppliedSeq){
    frames=[];freeze=null;viewEnd=null;markerA=markerB=hoverTime=null;cutoff=0;sampleDrops=0;epoch=data.epoch;lastAppliedSeq=0;
    pauseResetPending=true;
  }
  const incoming=data.frames||[];
  // 只追加未应用过的帧：SSE 与轮询回退切换时响应可能重叠，避免重复入缓存。
  const fresh=incoming.filter(frame=>frame.seq>lastAppliedSeq);
  cursor=data.cursor;serverNow=data.now;lastPoll=performance.now();lastData=data;
  if(data.capacity&&Number.isFinite(data.capacity))frameCap=Math.max(10000,Math.min(500000,data.capacity));
  if(data.dropped)sampleDrops+=data.dropped;
  if(fresh.length){
    const now=snapshotNow(data);
    // 高频路径不能每个事件都对整个缓存排序：SSE 增量通常已按发送端时间有序，
    // 只有检测到乱序才排序；裁剪也改为二分后切片，避免 O(n) 的 shift 循环。
    let ordered=frames.length===0||frames[frames.length-1].t<=fresh[0].t;
    if(ordered)for(let i=1;i<fresh.length;i++){if(fresh[i].t<fresh[i-1].t){ordered=false;break}}
    frames.push(...fresh);
    if(!ordered)frames.sort((a,b)=>a.t-b.t||a.seq-b.seq);
    if(frames.length>frameCap)frames=frames.slice(frames.length-frameCap);
    const limit=now-120;
    if(frames.length&&frames[0].t<limit){
      let lo=0,hi=frames.length;
      while(lo<hi){const mid=(lo+hi)>>1;if(frames[mid].t<limit)lo=mid+1;else hi=mid}
      if(lo>0)frames=frames.slice(lo);
    }
  }
  lastAppliedSeq=data.cursor;
  pendingCards=data;
  requestDraw();
}
async function poll(){
  if(streamConnected)return;
  try{
    const response=await fetch(`/api/data?since=${cursor}`,{signal:timeoutSignal(4000)});
    if(!response.ok)throw new Error(`HTTP ${response.status}`);
    const data=await response.json();
    if(streamConnected)return;  // SSE 已重连：丢弃这次可能更旧的轮询响应
    applyData(data);
  }catch(error){$('status').textContent='监听服务未连接';$('status-dot').className='bad';$('source').textContent='请确认 monitor.py 正在运行'}
  if(!streamConnected)pollTimer=setTimeout(poll,100);
}
function startFallbackPoll(){
  updateTransport(false);
  if(pollTimer||streamConnected)return;
  pollTimer=setTimeout(poll,100);
}
function stopFallbackPoll(){if(pollTimer){clearTimeout(pollTimer);pollTimer=null}}
function handleStreamPayload(raw){
  const data=window.ServalSignals.parseSnapshot(raw);
  if(!data)return;
  pushEvents++;pushCount++;lastPushAt=performance.now();
  applyData(data);
}
function scheduleStreamRetry(){
  if(streamRetry)return;
  streamRetry=setTimeout(()=>{streamRetry=null;startStream()},2000);
}
function startStream(){
  if(typeof EventSource==='undefined'){startFallbackPoll();return}
  if(stream)return;
  try{stream=new EventSource('/api/stream?since='+encodeURIComponent(cursor))}
  catch(error){stream=null;startFallbackPoll();scheduleStreamRetry();return}
  const onSnapshot=event=>{
    if(!event||typeof event.data!=='string')return;
    if(!streamConnected){streamConnected=true;updateTransport(true);stopFallbackPoll()}
    handleStreamPayload(event.data);
  };
  // 兼容后端两种写法：默认 message 事件，或 `event: data` 命名事件。
  stream.onmessage=onSnapshot;
  stream.addEventListener('data',onSnapshot);
  stream.onopen=()=>{streamConnected=true;updateTransport(true);stopFallbackPoll()};
  stream.onerror=()=>{
    // 旧后端（无 /api/stream）或断线：关闭并回退到旧轮询，稍后重试 SSE。
    if(stream){stream.close();stream=null}
    streamConnected=false;
    startFallbackPoll();
    scheduleStreamRetry();
  };
}
async function loadInitialSnapshot(){
  try{
    const response=await fetch(`/api/data?since=0`,{signal:timeoutSignal(10000)});
    if(!response.ok)throw new Error(`HTTP ${response.status}`);
    applyData(await response.json());
  }catch(error){
    $('status').textContent='监听服务未连接';$('status-dot').className='bad';$('source').textContent='请确认 monitor.py 正在运行';
  }
  // SSE 连接建立后不推初始快照，所以先取一次完整快照，再以当前 cursor 建立增量流。
  startStream();
}
window.addEventListener('pagehide',()=>{if(stream)stream.close()});
$('camera-feed').addEventListener('load',()=>{$('camera-placeholder').hidden=true});
$('camera-feed').addEventListener('error',()=>{
  $('camera-placeholder').hidden=false;$('camera-placeholder').textContent='相机画面暂不可用，等待重连';
  if(!$('camera-panel').hidden&&cameraRequested)setTimeout(()=>{if(!$('camera-panel').hidden&&cameraRequested)$('camera-feed').src='/api/camera.mjpg?t='+Date.now()},2000);
});
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
function syncDetailModeUi(){
  $('detail-mode').classList.toggle('active',detailMode);
  $('detail-mode').textContent=detailMode?'细节：逐点 开':'细节：逐点';
  $('detail-note').hidden=!detailMode;
}
$('detail-mode').addEventListener('click',()=>{detailMode=!detailMode;syncDetailModeUi();drawAll()});
$('duplex-window').addEventListener('change',ev=>{duplexWindow=Math.max(.05,Number(ev.target.value)||.5);drawDuplex()});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)requestDraw()});
syncDetailModeUi();
document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>{
  document.querySelectorAll('[data-view]').forEach(b=>{b.classList.toggle('selected',b===button);b.setAttribute('aria-selected',String(b===button))});
  document.querySelectorAll('.view-panel').forEach(panel=>panel.hidden=panel.id!=='view-'+button.dataset.view);
  drawAll();requestAnalysis();
}));
setInterval(()=>{if(!freeze)requestAnalysis()},1500);
$('imu').addEventListener('toggle',drawAll);window.addEventListener('resize',drawAll);
// 滚动时补画新进入视口的波形（离屏 canvas 被跳过以降低高采样率下的绘制负载）。
window.addEventListener('scroll',()=>requestDraw(),{passive:true});
document.querySelectorAll('[data-channel]').forEach(button=>button.addEventListener('click',()=>{
  channel=button.dataset.channel;document.querySelectorAll('[data-channel]').forEach(b=>b.classList.toggle('selected',b===button));
  charts.filter(c=>c.group).forEach(c=>c.box.hidden=channel!=='all'&&c.group!==channel);
  $('channel-note').textContent=channel==='accel'?'加速度只有 TX 实际下发值；协议不含 RX 加速度，不以微分估计冒充回传值':channel==='error'?'辅助计算量：此前实际 TX 减去当前 RX，Yaw 取最短角差；只统计控制段':'含 mode=0 原始下发值；不控制段与控制段断开显示';
  drawAll();
}));
async function refreshPorts(){
  try{const ports=await(await fetch('/api/ports',{signal:timeoutSignal(4000)})).json();clearNode($('ports'));
    for(const port of ports){const option=document.createElement('option');option.value=port.device;option.label=port.description;$('ports').append(option)}
  }catch(error){$('connection-error').hidden=false;$('connection-error').textContent='无法读取串口列表，请确认监听服务已启动'}
}
function udpModeBlocked(){
  if(lastData?.mode!=='live_udp')return false;
  $('connection-error').hidden=false;
  $('connection-error').textContent='UDP 实时接收模式下不能进入桥接、只读监听或离线回放；请停止 monitor.py 并选择所需数据源重启。';
  return true;
}
async function control(path,args={}){
  if(udpModeBlocked())return;
  busy=true;$('connect').disabled=true;$('disconnect').disabled=true;$('connection-error').hidden=true;
  try{const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(args)});const result=await response.json();if(!response.ok)throw new Error(result.error||'串口操作失败')}
  catch(error){$('connection-error').hidden=false;$('connection-error').textContent=error.message}
  finally{busy=false}
}
$('connect-form').addEventListener('submit',ev=>{ev.preventDefault();if(udpModeBlocked())return;control('/api/start',{port:$('physical-port').value.trim(),baud:+$('baud').value,virtual:$('virtual-port').value.trim(),link:$('link-path').value.trim(),record:$('record-enabled').checked})});
$('disconnect').addEventListener('click',()=>control('/api/stop'));
$('refresh-ports').addEventListener('click',refreshPorts);
$('follow-form').addEventListener('submit',ev=>{ev.preventDefault();if(udpModeBlocked())return;control('/api/follow',{path:$('follow-path').value.trim()})});
$('follow-stop').addEventListener('click',()=>control('/api/stop'));
$('color-mapping').addEventListener('change',()=>{if(lastData)updateCards({...lastData,dropped:0})});
$('replay-play').addEventListener('click',()=>control('/api/replay/control',{action:lastData?.replay?.playing?'pause':'play'}));
$('replay-restart').addEventListener('click',()=>control('/api/replay/control',{action:'restart'}));
$('replay-speed').addEventListener('change',ev=>control('/api/replay/control',{action:'speed',speed:+ev.target.value}));
$('replay-stop').addEventListener('click',()=>control('/api/stop'));
$('load-replay').addEventListener('click',async()=>{
  if(udpModeBlocked())return;
  const file=$('replay-file').files[0];
  if(!file){$('connection-error').hidden=false;$('connection-error').textContent='请先选择原工程 .bin 日志';return}
  busy=true;$('connection-error').hidden=true;
  try{
    const response=await fetch('/api/replay/file',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-File-Name':encodeURIComponent(file.name)},body:file});
    const result=await response.json();if(!response.ok)throw new Error(result.error||'日志加载失败');
  }catch(error){$('connection-error').hidden=false;$('connection-error').textContent=error.message}
  finally{busy=false}
});
/* 只读诊断钩子：供 QtWebEngine 冒烟测试 / E2E 断言使用，不改变页面行为。
   同时导出 window.__SERVAL_DEBUG__ 快照对象和 window.ServalDesktop 桌面窗口标记。 */
function servalDebug(){
  const camera=document.getElementById('camera-panel'),tools=document.getElementById('native-tools');
  const udp=lastData&&lastData.udp?window.ServalSignals.udpCounters(lastData.udp):null;
  return {
    frames: frames.length,
    cursor: cursor,
    serverNow: serverNow,
    displayNow: snapshotNow(lastData),
    transport: streamConnected?'sse':(pollTimer?'poll':'idle'),
    mode: lastData?lastData.mode:null,
    epoch: epoch,
    retained: lastData?lastData.retained:0,
    latestT: lastData&&lastData.latest?Math.max(lastData.latest.tx?.t??-Infinity,lastData.latest.rx?.t??-Infinity):null,
    udp: udp?{missing:udp.missing,late:udp.late,invalid:udp.invalid,peer:udp.peer,age:udp.age,fps:udp.fps,session:udp.session}:null,
    nativeAvailable: !!(window.ServalNative&&window.ServalNative.isAvailable&&window.ServalNative.isAvailable()),
    nativeToolsVisible: !!tools&&!tools.hidden,
    cameraVisible: !!camera&&!camera.hidden,
    cameraCollapsed: cameraCollapsed,
    /* 第二轮高频/细节诊断 */
    drawFps: drawFps,
    pushEvents: pushEvents,
    pushHz: pushHz,
    lastPushAgeMs: lastPushAt===null?null:Math.max(0,performance.now()-lastPushAt),
    detailMode: detailMode,
    gaps: lastDetailGaps.map(gap=>({t:gap.t,ms:gap.ms,direction:gap.direction})),
    duplexWindow: duplexWindow,
    pushHzTarget: Number.isFinite(lastData&&lastData.push_hz)?lastData.push_hz:null,
    hidden: !!document.hidden,
    status: document.getElementById('status').textContent
  };
}
function refreshDebugSnapshot(){
  const snapshot=servalDebug();
  window.__SERVAL_DEBUG__=snapshot;
  try{if(typeof window.ServalDebug==='function')Object.assign(window.ServalDebug,snapshot)}catch(error){}
  window.ServalDesktop={mode:'desktop-webengine',native:snapshot.nativeAvailable,
    browser:!snapshot.nativeAvailable,transport:snapshot.transport};
}
window.ServalDebug=servalDebug;
refreshDebugSnapshot();
refreshPorts();
loadInitialSnapshot();
