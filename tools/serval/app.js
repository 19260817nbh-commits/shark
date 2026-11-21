'use strict';
const $ = id => document.getElementById(id);
const colors = {tx:'#cf8135', rx:'#148a86', imu:'#8a77bd', second:'#628ec5', third:'#cf8135', fourth:'#148a86'};
let frames=[], cursor=0, serverNow=0, lastPoll=performance.now(), unit='deg', windowSec=10;
let freeze=null, cutoff=0, latest={}, charts=[], sampleDrops=0, epoch=null, busy=false;
const series=(direction,key,label,color=colors[direction])=>({direction,key,label,color,visible:true});
const chartDefs=[
  ['Yaw','偏航角','angle',[series('tx','yaw','目标'),series('rx','yaw','反馈')]],
  ['Pitch','俯仰角','angle',[series('tx','pitch','目标'),series('rx','pitch','反馈')]],
  ['Yaw velocity','偏航角速度','rate',[series('tx','yaw_vel','目标'),series('rx','yaw_vel','反馈')]],
  ['Pitch velocity','俯仰角速度','rate',[series('tx','pitch_vel','目标'),series('rx','pitch_vel','反馈')]],
];
const imuDefs=[
  ['IMU attitude','姿态欧拉角','angle',[series('rx','q_yaw','Yaw',colors.imu),series('rx','q_pitch','Pitch',colors.second),series('rx','q_roll','Roll',colors.third)]],
  ['Quaternion','四元数 · wxyz','scalar',[series('rx','qw','w',colors.imu),series('rx','qx','x',colors.second),series('rx','qy','y',colors.third),series('rx','qz','z',colors.fourth)]],
  ['Yaw acceleration','偏航角加速度','accel',[series('tx','yaw_acc','目标')]],
  ['Pitch acceleration','俯仰角加速度','accel',[series('tx','pitch_acc','目标')]],
];

function createChart(def,container){
  const [name,subtitle,kind,items]=def;
  const box=document.createElement('section');box.className='chart';
  box.innerHTML='<div class="chart-head"><h3></h3><div class="legend"></div></div><div class="chart-value">等待数据</div><canvas></canvas><div class="chart-bottom"><span></span><span>自动缩放 · 悬停读数</span></div>';
  box.querySelector('h3').append(document.createTextNode(name));
  const small=document.createElement('small');small.textContent=subtitle;box.querySelector('h3').append(small);
  const chart={box,kind,items,canvas:box.querySelector('canvas'),hover:null};
  for(const item of items){
    const label=document.createElement('label'), input=document.createElement('input');input.type='checkbox';input.checked=true;input.style.setProperty('--color',item.color);
    input.setAttribute('aria-label',`${name} ${item.label}`);
    input.addEventListener('change',()=>{item.visible=input.checked;drawAll()});
    label.append(input,document.createTextNode(item.label));box.querySelector('.legend').append(label);
  }
  chart.canvas.setAttribute('aria-label',`${name} 实时曲线`);
  chart.canvas.addEventListener('mousemove',ev=>{chart.hover=ev.offsetX;drawAll()});
  chart.canvas.addEventListener('mouseleave',()=>{chart.hover=null;drawAll()});
  container.append(box);charts.push(chart);
}
chartDefs.forEach(def=>createChart(def,$('charts')));imuDefs.forEach(def=>createChart(def,$('imu-charts')));

function scaleValue(value,kind){return kind!=='scalar'&&unit==='deg'?value*180/Math.PI:value}
function unitText(kind){return kind==='scalar'?'无量纲':(unit==='deg'?'°':'rad')+({angle:'',rate:'/s',accel:'/s²'}[kind])}
function fmt(n,digits=2){return Number.isFinite(n)?n.toFixed(digits):'—'}
function numberLabel(n){if(Math.abs(n)>=10000)return n.toExponential(1);return n.toFixed(Math.abs(n)<.1?3:Math.abs(n)<10?2:1)}

// Envelope reduction retains both extremes in every pixel column, including narrow spikes.
function reduce(points,width){
  if(points.length<width*2)return points;
  const result=[];let bucket=-1,min=null,max=null;
  const flush=()=>{if(min){if(min.t<=max.t)result.push(min,max);else result.push(max,min)}};
  for(const p of points){const b=Math.floor(p.x);if(b!==bucket){flush();bucket=b;min=max=p}else{if(p.y<min.y)min=p;if(p.y>max.y)max=p}}
  flush();return result;
}

function drawChart(chart,source,end){
  const {canvas,kind,items}=chart;
  const rect=canvas.getBoundingClientRect();if(!rect.width||!rect.height)return;
  const ratio=window.devicePixelRatio||1,w=rect.width,h=rect.height;
  if(canvas.width!==Math.round(w*ratio)||canvas.height!==Math.round(h*ratio)){canvas.width=Math.round(w*ratio);canvas.height=Math.round(h*ratio)}
  const ctx=canvas.getContext('2d');ctx.setTransform(ratio,0,0,ratio,0,0);ctx.clearRect(0,0,w,h);
  const left=57,right=w-19,top=13,bottom=h-31,start=end-windowSec;
  const visible=items.filter(s=>s.visible);
  let lo=Infinity,hi=-Infinity,total=0;
  const datasets=visible.map(s=>{
    const points=[];
    for(const f of source){if(f.t<start||f.t>end||f.t<cutoff||f.direction!==s.direction)continue;
      const raw=f.values[s.key];if(!Number.isFinite(raw))continue;
      const value=scaleValue(raw,kind);lo=Math.min(lo,value);hi=Math.max(hi,value);total++;points.push({t:f.t,value});
    }
    return {series:s,points};
  });
  if(!Number.isFinite(lo)){lo=-1;hi=1}
  const margin=Math.max((hi-lo)*.12,kind==='scalar'?.05:(unit==='deg'?.25:.005));lo-=margin;hi+=margin;
  const x=t=>left+(t-start)/windowSec*(right-left), y=v=>bottom-(v-lo)/(hi-lo)*(bottom-top);
  ctx.font='10px Consolas, monospace';ctx.lineWidth=1;
  for(let i=0;i<5;i++){
    const yy=top+(bottom-top)*i/4;ctx.strokeStyle='#edf1f4';ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(right,yy);ctx.stroke();
    ctx.fillStyle='#9ba7b0';ctx.textAlign='right';ctx.fillText(numberLabel(hi-(hi-lo)*i/4),left-9,yy+3);
  }
  for(let i=0;i<5;i++){
    const xx=left+(right-left)*i/4;ctx.strokeStyle='#f3f5f7';ctx.beginPath();ctx.moveTo(xx,top);ctx.lineTo(xx,bottom);ctx.stroke();
    ctx.fillStyle='#9ba7b0';ctx.textAlign='center';ctx.fillText(`${(-windowSec+windowSec*i/4).toFixed(windowSec===5?1:0)}s`,xx,bottom+19);
  }
  ctx.save();ctx.beginPath();ctx.rect(left,top,right-left,bottom-top);ctx.clip();
  for(const {series:s,points} of datasets){
    const path=reduce(points.map(p=>({...p,x:x(p.t),y:y(p.value)})),right-left);
    ctx.strokeStyle=s.color;ctx.lineWidth=1.6;ctx.beginPath();let last=null;
    for(const p of path){if(last===null||p.t-last>.5)ctx.moveTo(p.x,p.y);else ctx.lineTo(p.x,p.y);last=p.t}ctx.stroke();
    if(path.length===1){ctx.fillStyle=s.color;ctx.beginPath();ctx.arc(path[0].x,path[0].y,2,0,Math.PI*2);ctx.fill()}
  }
  let hoverTime=null;
  if(chart.hover!==null&&chart.hover>=left&&chart.hover<=right){
    hoverTime=start+(chart.hover-left)/(right-left)*windowSec;
    ctx.setLineDash([3,3]);ctx.strokeStyle='#a9b4bc';ctx.beginPath();ctx.moveTo(chart.hover,top);ctx.lineTo(chart.hover,bottom);ctx.stroke();ctx.setLineDash([]);
  }
  ctx.restore();
  if(!total){ctx.fillStyle='#a1adb5';ctx.font='12px "Microsoft YaHei", sans-serif';ctx.textAlign='center';ctx.fillText(visible.length?'等待此通道的数据':'选择上方信号以显示曲线',(left+right)/2,(top+bottom)/2)}
  const values=datasets.map(({series:s,points})=>{
    let p=points[points.length-1];
    if(hoverTime!==null&&points.length)p=points.reduce((a,b)=>Math.abs(a.t-hoverTime)<Math.abs(b.t-hoverTime)?a:b);
    return `${s.label} ${p?fmt(p.value,3):'—'}`;
  });
  chart.box.querySelector('.chart-value').textContent=values.join('   /   ')||'未选择信号';
  chart.box.querySelector('.chart-bottom span').textContent=`${unitText(kind)} · ${total.toLocaleString()} 个采样`;
}

function drawAll(){
  const now=freeze?freeze.now:serverNow+(lastData?.mode==='replay'?0:(performance.now()-lastPoll)/1000);
  const source=freeze?freeze.frames:frames;
  for(const chart of charts)drawChart(chart,source,now);
}

function updateCards(data){
  latest=data.latest;
  const rx=latest.rx,tx=latest.tx;
  const last=Math.max(rx?.t??-Infinity,tx?.t??-Infinity);
  const age=data.now-last;
  $('status').textContent=data.demo?'演示运行中':data.mode==='replay'?(data.replay.playing?'离线回放中':data.replay.position>=data.replay.duration?'回放完成':'回放已暂停'):data.bridge.error?'串口已断开':data.mode==='idle'?'未连接串口':age<2?'数据接收中':last===-Infinity?'等待通信数据':'数据已中断';
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
  $('connect').disabled=busy||bridge.running||data.demo||data.mode==='replay';
  $('disconnect').disabled=busy||!bridge.running||data.demo;
  for(const id of ['physical-port','baud','virtual-port','link-path','record-enabled'])$(id).disabled=bridge.running||data.demo||data.mode==='replay';
  if(bridge.running&&!$('physical-port').value)$('physical-port').value=bridge.physical;
  $('app-port-line').hidden=!bridge.running;
  $('app-port').textContent=bridge.app_port;
  $('bridge-hint').textContent=data.demo?'当前为模拟数据；实际协议为 TX 30 / RX 44 字节。连接设备或回放日志时请重新启动，不带 --demo。':bridge.running?'桥接正在运行。上位机使用上方虚拟串口；停止桥接会中断通信，设备断开后需手动重连。':bridge.windows?'Windows：先准备虚拟串口对。此处填写工具端 COM，上位机打开另一端。':'Linux：使用固定路径 /tmp/serval-gimbal，首次把上位机 com_port 改为该路径；多实例可指定不同路径。';
  if(bridge.error){$('connection-error').hidden=false;$('connection-error').textContent=bridge.error}
  $('target-id').textContent=tx?.values.target_id??'—';
  $('enemy-color').textContent=({0:'蓝方',1:'红方'})[rx?.values.enemy_color]??'—';
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
    if(epoch!==data.epoch){frames=[];freeze=null;cutoff=0;sampleDrops=0;epoch=data.epoch;$('pause').textContent='Ⅱ 暂停曲线';$('pause').classList.remove('active')}
    cursor=data.cursor;serverNow=data.now;lastPoll=performance.now();lastData=data;
    frames.push(...data.frames);if(frames.length>60000)frames.splice(0,frames.length-60000);
    while(frames.length&&frames[0].t<serverNow-120)frames.shift();
    updateCards(data);drawAll();
  }catch(error){$('status').textContent='监听服务未连接';$('status-dot').className='bad';$('source').textContent='请确认 monitor.py 正在运行'}
  setTimeout(poll,100);
}
$('pause').addEventListener('click',()=>{
  freeze=freeze?null:{frames:frames.slice(),now:serverNow+(lastData?.mode==='replay'?0:(performance.now()-lastPoll)/1000)};
  $('pause').textContent=freeze?'▶ 继续曲线':'Ⅱ 暂停曲线';$('pause').classList.toggle('active',!!freeze);drawAll();
});
$('clear').addEventListener('click',()=>{cutoff=serverNow;frames=[];if(freeze)freeze={frames:[],now:serverNow};drawAll()});
$('window').addEventListener('change',ev=>{windowSec=+ev.target.value;drawAll()});
$('unit').addEventListener('change',ev=>{unit=ev.target.value;if(lastData)updateCards({...lastData,dropped:0});drawAll()});
$('imu').addEventListener('toggle',drawAll);window.addEventListener('resize',drawAll);
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
