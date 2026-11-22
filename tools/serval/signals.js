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
  const api={wrap,points,prior,errorPoints,envelope};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;else window.ServalSignals=api;
})();
