'use strict';
(() => {
  const get=id=>document.getElementById(id);
  /* Chromium 83（QtWebEngine 5.15）没有 AbortSignal.timeout；统一兼容 helper。 */
  const timeoutSignal=ms=>{
    if(typeof AbortSignal!=='undefined'&&typeof AbortSignal.timeout==='function')return AbortSignal.timeout(ms);
    const controller=new AbortController();setTimeout(()=>controller.abort(),ms);return controller.signal;
  };
  const modes={tx:['不控制','控制 / 不开火','控制 / 开火'],rx:['空闲','自瞄','小符','大符']};
  const shared=[['mode',2,1,'模式']];
  const fields={
    tx:[...shared,['yaw',3,4,'目标偏航'],['yaw_vel',7,4,'目标偏航速度'],['yaw_acc',11,4,'目标偏航加速度'],
      ['pitch',15,4,'目标俯仰'],['pitch_vel',19,4,'目标俯仰速度'],['pitch_acc',23,4,'目标俯仰加速度'],['target_id',27,1,'目标 ID']],
    rx:[...shared,['qw',3,4,'四元数 w'],['qx',7,4,'四元数 x'],['qy',11,4,'四元数 y'],['qz',15,4,'四元数 z'],
      ['yaw',19,4,'实际偏航'],['yaw_vel',23,4,'实际偏航速度'],['pitch',27,4,'实际俯仰'],
      ['pitch_vel',31,4,'实际俯仰速度'],['bullet_speed',35,4,'弹速'],['bullet_count',39,2,'累计发弹数'],['enemy_color',41,1,'颜色原值']]
  };
  let frozen=false, lastData=null, failed=false, pollDelay=500;
  const pollInterval=()=>lastData&&lastData.mode==='live_udp'?200:500;
  const refreshText=()=>'刷新 '+Math.round(1000/pollDelay)+' Hz（'+pollDelay+' ms）';
  const node=(tag,text,className)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(className)n.className=className;return n};
  const hex=n=>n===null?'—':'0x'+n.toString(16).toUpperCase().padStart(4,'0');
  const stamp=p=>p.t.toFixed(6)+' s · 日志序号 '+(p.log_seq??'未记录')+
    (p.log_time_ns?' · 原记录 '+p.log_time_ns+' ns':'');
  function fieldValue(key,value,direction){
    if(key==='mode')return String(value)+' · '+(modes[direction][value]??'未知模式');
    if(key==='enemy_color')return String(value)+' · 原始字节，不推断红蓝';
    if(Number.isInteger(value)&&['target_id','bullet_count'].includes(key))return String(value);
    if(typeof value!=='number')return '—';
    if(key==='bullet_speed')return value.toPrecision(9)+' m/s';
    if(key.startsWith('q'))return value.toPrecision(9);
    const unit=key.endsWith('_acc')?'rad/s²':key.endsWith('_vel')?'rad/s':'rad';
    return value.toPrecision(9)+' '+unit+' · '+(value*180/Math.PI).toFixed(6)+' '+unit.replace('rad','°');
  }
  function card(direction,p){
    const box=get('packet-'+direction);
    const wasOpen=box.querySelector('details')?.open??false;
    box.textContent='';
    box.append(node('h3',direction==='tx'?'TX · 上位机指令（30 字节）':'RX · 云台反馈（44 字节）'));
    if(!p){box.append(node('p','尚未收到该方向的完整包'));return}
    const meta=node('p',stamp(p)+' · '+p.length+' 字节');
    const badge=node('strong',p.crc_ok?'CRC OK':'CRC BAD',p.crc_ok?'packet-good':'packet-bad');
    box.append(meta,badge,node('pre',p.hex,'packet-hex'));
    box.append(node('p','接收 CRC '+hex(p.crc_received)+' / 计算 CRC '+hex(p.crc_calculated),'packet-note'));
    if(p.log_status!==null){
      const status=p.log_status;
      box.append(node('p','日志 status=0x'+status.toString(16).padStart(2,'0')+
        (status&4?' · I/O 异常':direction==='tx'?(status&2?' · 写入返回无异常':' · 未标记写入成功'):(status&2?' · 记录端校验通过':' · 记录端未标记校验通过')),'packet-note'));
    }
    if(p.error){box.append(node('p',p.error+'；下方只保留原始字节，不把它作为有效反馈。','packet-bad'));return}
    const details=node('details');details.open=wasOpen;
    details.append(node('summary','逐字段解析（偏移从 0 开始）'));
    const table=node('table',undefined,'packet-table'),head=node('tr');
    ['字段','偏移 / 长度','原始 HEX','解析值'].forEach(t=>head.append(node('th',t)));
    const thead=node('thead');thead.append(head);table.append(thead);
    const body=node('tbody'),bytes=p.hex.split(' ');
    const rows=[['head',0,2,'帧头'],...fields[direction],['crc16',p.length-2,2,'CRC16']];
    for(const [key,offset,size,label] of rows){
      const row=node('tr');
      const value=key==='head'?'SP':key==='crc16'?hex(p.crc_received):fieldValue(key,p.values?.[key],direction);
      [label+' / '+key,offset+' / '+size,bytes.slice(offset,offset+size).join(' '),value].forEach(t=>row.append(node('td',t)));
      body.append(row);
    }
    table.append(body);const scroll=node('div',undefined,'packet-scroll');scroll.append(table);details.append(scroll);box.append(details);
  }
  function render(data){
    card('tx',data.latest.tx);card('rx',data.latest.rx);
    const tx=data.latest.tx,rx=data.latest.rx;
    const age=p=>p?Math.max(0,data.now-p.t).toFixed(2)+' 秒前':'未收到';
    get('packet-status').textContent=(data.mode==='replay'?'离线回放':'TX '+age(tx)+' · RX '+age(rx))+
      ' · 原始包缓存 '+data.retained+'/'+data.capacity+' · '+refreshText();
    const download=data.download??{available:false};
    get('packet-download').setAttribute('aria-disabled',String(!download.available));
    get('packet-download').textContent=download.scope==='log'?'保存当前日志（.bin）':'保存缓存包（.bin）';
    get('packet-download-note').textContent=download.scope==='log'?
      '保存到浏览器所在电脑：当前日志已完整解析的全部收发包（含坏 CRC），不受最近 30 包显示限制；下载期间新增的数据留待下次保存。':
      '当前没有可下载的日志文件，将保存最近最多 1,000 个缓存包（含坏 CRC）。解析表格可使用页面顶部“导出 CSV”。';
    const body=get('packet-rows');body.textContent='';
    for(const p of data.packets){
      const row=node('tr');
      [p.log_seq??'—',p.t.toFixed(6),p.direction.toUpperCase(),p.length,p.crc_ok?'OK':'BAD',p.hex].forEach((v,i)=>{
        const cell=node('td',String(v),i===5?'packet-hex-cell':undefined);row.append(cell);
      });
      if(!p.crc_ok)row.className='packet-bad';
      body.append(row);
    }
  }
  async function poll(){
    try{
      if(!frozen){
        const response=await fetch('/api/packets?direction='+get('packet-direction').value+'&limit=30',{signal:timeoutSignal(4000)});
        if(!response.ok)throw new Error('HTTP '+response.status);
        lastData=await response.json();
        // live_udp 高频模式下加快刷新；其他模式保持 500 ms 低干扰。
        pollDelay=pollInterval();
        render(lastData);failed=false;
      }
    }catch(error){if(!failed)get('packet-status').textContent='数据包服务未连接：'+error.message;failed=true}
    setTimeout(poll,pollDelay);
  }
  get('packet-pause').addEventListener('click',()=>{
    frozen=!frozen;get('packet-pause').textContent=frozen?'继续数据包显示':'暂停数据包显示';
    get('packet-pause').classList.toggle('active',frozen);
  });
  get('packet-direction').addEventListener('change',()=>{if(frozen&&lastData)render({...lastData,packets:lastData.packets.filter(p=>get('packet-direction').value==='both'||p.direction===get('packet-direction').value)})});
  get('packet-download').addEventListener('click',event=>{
    if(get('packet-download').getAttribute('aria-disabled')==='true')event.preventDefault();
  });
  poll();
})();
