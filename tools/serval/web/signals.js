/* Pure signal operations shared by the chart and deterministic Node tests. */
(() => {
  const wrap=v=>Math.atan2(Math.sin(v),Math.cos(v));
  function points(frames,direction,key,continuous=false){
    let prev=null,value=0,last=null,segment=0,lastMode=null;
    const result=[];
    for(const f of frames.filter(f=>f.direction===direction&&Number.isFinite(f.values[key])).sort((a,b)=>a.t-b.t)){
      const raw=f.values[key];
      if(direction==='tx'&&lastMode!==null&&lastMode!==f.values.mode){segment++;prev=null}
      lastMode=f.values.mode;
      if(last!==null&&f.t-last>.1){segment++;prev=null}
      const isAngle=['yaw','q_yaw','q_roll'].includes(key);
      if(!continuous&&isAngle&&prev!==null&&Math.abs(raw-prev)>Math.PI)segment++;
      value=continuous&&isAngle&&prev!==null?value+wrap(raw-prev):raw;
      prev=raw;last=f.t;
      result.push({t:f.t,value,raw,segment,seq:f.seq,mode:f.values.mode});
    }
    return result;
  }
  function prior(points,t,maxAge=.05){
    let lo=0,hi=points.length;
    while(lo<hi){const m=(lo+hi)>>1;if(points[m].t<=t)lo=m+1;else hi=m}
    const p=points[lo-1];return p&&t-p.t<=maxAge?p:null;
  }
  function errorPoints(frames,axis){
    const tx=frames.filter(f=>f.direction==='tx'&&Number.isFinite(f.values[axis])).sort((a,b)=>a.t-b.t)
      .map(f=>({t:f.t,raw:f.values[axis],mode:f.values.mode}));
    const rx=points(frames,'rx',axis),result=[];
    let segment=0,last=null;
    for(const p of rx){
      const cmd=prior(tx,p.t);
      if(!cmd||![1,2].includes(cmd.mode)){segment++;last=null;continue}
      if(last!==null&&p.t-last>.1)segment++;
      result.push({t:p.t,value:axis==='yaw'?wrap(cmd.raw-p.raw):cmd.raw-p.raw,segment,age:p.t-cmd.t});last=p.t;
    }
    return result;
  }
  function envelope(points,width,x){
    if(points.length<width*4)return points;
    const result=[];let bucket=null,items=[];
    const flush=()=>{
      if(!items.length)return;
      const chosen=new Set([0,items.length-1]);let min=0,max=0;
      items.forEach((p,i)=>{if(p.value<items[min].value)min=i;if(p.value>items[max].value)max=i});
      chosen.add(min);chosen.add(max);[...chosen].sort((a,b)=>a-b).forEach(i=>result.push(items[i]));
    };
    for(const p of points){const b=Math.floor(x(p.t))+':'+p.segment;if(b!==bucket){flush();items=[];bucket=b}items.push(p)}
    flush();return result;
  }
  /* Parse an SSE `data:` payload; returns a plain object snapshot or null. */
  function parseSnapshot(text){
    if(typeof text!=='string'||!text)return null;
    try{
      const data=JSON.parse(text);
      return data&&typeof data==='object'&&!Array.isArray(data)?data:null;
    }catch(error){return null}
  }
  /* Normalise Application.snapshot()['udp'] counters for the GUI; tolerant to
     numbers, {tx,rx} objects or per-direction arrays and to `bad` vs `invalid`. */
  function udpCounters(udp){
    const result={missing:0,late:0,invalid:0,peer:'—',error:'',age:null,fps:null,fpsAge:null,session:null,running:null};
    if(!udp||typeof udp!=='object')return result;
    const total=(...names)=>{
      for(const name of names){
        const value=udp[name];
        if(Number.isFinite(value))return value;
        if(Array.isArray(value))return value.reduce((sum,item)=>sum+(Number.isFinite(item)?item:0),0);
        if(value&&typeof value==='object'){
          const numbers=Object.keys(value).map(key=>value[key]).filter(Number.isFinite);
          if(numbers.length)return numbers.reduce((sum,item)=>sum+item,0);
        }
      }
      return null;
    };
    const number=(...names)=>{
      for(const name of names)if(Number.isFinite(udp[name]))return udp[name];
      return null;
    };
    result.missing=total('missing','missing_count')??0;
    result.late=total('late','late_count')??0;
    result.invalid=total('invalid','bad','invalid_count')??0;
    result.age=number('age');
    result.fps=number('fps');
    result.fpsAge=number('fps_age');
    if(Number.isFinite(udp.session))result.session=udp.session;
    if(typeof udp.running==='boolean')result.running=udp.running;
    if(typeof udp.error==='string')result.error=udp.error;
    const peer=udp.peer;
    if(typeof peer==='string'&&peer)result.peer=peer;
    else if(peer&&typeof peer==='object'){
      const host=peer.ip??peer.host??peer.addr;
      if(host)result.peer=String(host)+(Number.isFinite(peer.port)?':'+peer.port:'');
    }
    return result;
  }
  /* Detail-mode helpers: mark abnormal sample spacing on a drawn series.
     `points` must be ascending by t (as produced by points()/errorPoints()).
     A mark is added where `segment` changes or the sample gap exceeds threshold ms. */
  function gapMarks(points, threshold=100){
    const result=[];
    if(!Array.isArray(points)||points.length<2)return result;
    let previous=points[0];
    for(let index=1;index<points.length;index++){
      const point=points[index];
      const deltaMs=(point.t-previous.t)*1000;
      if(point.segment!==previous.segment||deltaMs>threshold){
        result.push({t:point.t,start:previous.t,ms:deltaMs,
          kind:point.segment!==previous.segment?'segment':'gap'});
      }
      previous=point;
    }
    return result;
  }
  /* Pair each RX sample with the latest TX sample not after it (age <= maxAge ms).
     Both lists must be ascending by t; used by the TX/RX duplex timing canvas and
     the TX→RX sample-offset statistic (computed from cached frames, no downsampling). */
  function duplexPairs(txPoints, rxPoints, maxAge=100){
    const pairs=[],tx=Array.isArray(txPoints)?txPoints:[],rx=Array.isArray(rxPoints)?rxPoints:[];
    let index=0;
    for(const point of rx){
      while(index+1<tx.length&&tx[index+1].t<=point.t)index++;
      const candidate=tx[index];
      if(!candidate||candidate.t>point.t)continue;
      const age=(point.t-candidate.t)*1000;
      if(age<=maxAge)pairs.push({tx:candidate,rx:point,age});
    }
    return pairs;
  }
  const api={wrap,points,prior,errorPoints,envelope,parseSnapshot,udpCounters,gapMarks,duplexPairs};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;else window.ServalSignals=api;
})();
